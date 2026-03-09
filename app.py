#!/usr/bin/env python3
"""
YouTube Shorts 自動切り抜き・投稿予約アプリ
1本の動画URL → 10本のShortsを自動選定 → 予約投稿
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
    try:
        raw_ck = st.secrets["youtube"]["cookies"]
    except Exception:
        raw_ck = os.environ.get("YOUTUBE_COOKIES")
    if raw_ck:
        raw_ck = raw_ck.strip()
        ck_path.write_text(raw_ck, encoding="utf-8")

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
    """
    params = st.query_params
    code = params.get("code")
    if not code:
        return False

    state = params.get("state", "")
    redirect_uri = _get_app_url()

    # state からユーザーIDと PKCE code_verifier を事前に取得
    user_id, code_verifier = _parse_oauth_state(state)

    try:
        from core.uploader import exchange_code
        token_json_str = exchange_code(code, redirect_uri, code_verifier=code_verifier)
        token_data = json.loads(token_json_str)
        if user_id and _is_multi_user_mode():
            from core.db import save_youtube_token
            save_youtube_token(user_id, token_json_str)
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
    Supabase メール確認リンクからのアクセストークンを処理してログインを確立する。
    URL fragment (#access_token=...) は JS で ?sb_access_token= に変換済み。
    """
    token         = st.query_params.get("sb_access_token", "")
    refresh_token = st.query_params.get("sb_refresh_token", "")

    if not token:
        st.query_params.clear()
        return

    try:
        from core.auth import get_supabase
        sb = get_supabase()

        # set_session でセッションを確立（両トークン使用）
        user = None
        try:
            session_resp = sb.auth.set_session(token, refresh_token)
            if session_resp and session_resp.user:
                user = session_resp.user
        except Exception:
            pass

        # フォールバック: アクセストークンだけで検証
        if not user:
            try:
                user_resp = sb.auth.get_user(token)
                if user_resp:
                    user = user_resp.user
            except Exception:
                pass

        if user:
            st.session_state["user_id"]    = user.id
            st.session_state["user_email"] = user.email
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
            st.warning("⚠️ メール認証リンクが無効か期限切れです。再度ログインしてください。")

    except Exception as e:
        st.query_params.clear()
        st.error(f"認証エラー: {e}")


def _redirect_to_url(url: str):
    """JavaScript でトップレベルウィンドウを指定 URL にリダイレクト"""
    import streamlit.components.v1 as _components
    # XSS 対策: url はサーバー生成値のみ渡す
    _components.html(
        f'<script>window.top.location.href = {json.dumps(url)};</script>',
        height=0,
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
    "small":  {"label": "S 小",  "font": "12px", "weight": "700", "lh": "1.4", "pad": "10px 14px 14px"},
    "medium": {"label": "M 中",  "font": "15px", "weight": "900", "lh": "1.45","pad": "14px 16px 18px"},
    "large":  {"label": "L 大",  "font": "18px", "weight": "900", "lh": "1.5", "pad": "16px 16px 22px"},
}
# タイトルバー高さ概算（catchphrase込み）
TITLE_BAR_H = {"small": 72, "medium": 92, "large": 112}

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
    ("small",  ["文字小", "小さめ", "コンパクト", "小文字"]),
    ("large",  ["文字大", "大きめ", "でかめ", "大文字"]),
    ("medium", ["文字中", "普通サイズ", "標準"]),
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
/* ログアウトボタン位置調整 */
div[data-testid="stButton"] button[title="ログアウトします"] {
  font-size:11px !important; padding:2px 8px !important;
  height:28px !important; border-radius:6px !important;
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
.done  .st-circle { background:#10b981; color:#fff; }
.done  .st-line   { background:#10b981; }
.active .st-circle { background:#ea580c; color:#fff; box-shadow:0 0 14px #ea580c88; }
.active .st-line   { background:linear-gradient(90deg,#ea580c50,#e5e7eb); }
.wait  .st-circle { background:#f3f4f6; color:#9ca3af; border:1px solid #d1d5db; }
.wait  .st-line   { background:#e5e7eb; }
.st-label { font-size:11px; font-weight:500; white-space:nowrap; }
.done  .st-label  { color:#059669; }
.active .st-label { color:#ea580c; font-weight:700; }
.wait  .st-label  { color:#9ca3af; }

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

/* クリップカード */
.clip-card {
  background:#ffffff; border:1px solid #e5e7eb;
  border-radius:14px; padding:0; margin:12px 0;
  overflow:hidden; transition:.2s; box-shadow:0 1px 4px rgba(0,0,0,0.06);
}
.clip-header {
  background:linear-gradient(90deg,#eef2ff,#f8faff);
  padding:12px 18px; display:flex; align-items:center; gap:12px;
}
.clip-num {
  background:#4f46e5; color:#fff; border-radius:50%;
  width:26px; height:26px; display:flex; align-items:center;
  justify-content:center; font-size:12px; font-weight:700; flex-shrink:0;
}
.clip-title-preview { font-size:13px; color:#3730a3; font-weight:500; }
.time-tag {
  background:#ede9fe; color:#5b21b6; border-radius:6px;
  padding:2px 9px; font-size:12px; font-family:monospace;
  border:1px solid #c4b5fd; margin-left:auto;
}
.transcript-box {
  background:#f8fafc; border-radius:8px; padding:10px 14px;
  font-size:12px; color:#64748b; line-height:1.65;
  max-height:56px; overflow:hidden; margin:0 18px 14px;
  border-left:3px solid #c4b5fd;
}
.no-transcript { font-style:italic; color:#9ca3af; }

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
  .block-container { padding:0 0 40px !important; }
  .app-header { padding:12px 16px !important; margin-left:-16px !important; margin-right:-16px !important; }
  .brand-logo { height:48px !important; }
  .brand-logo-fallback { width:48px !important; height:48px !important; font-size:28px !important; }
  .brand-name { font-size:18px !important; }
  .step-area { padding:14px 16px 0 !important; margin-left:-16px !important; margin-right:-16px !important; }
  .st-circle { width:28px !important; height:28px !important; font-size:12px !important; }
  .st-line { min-width:16px !important; }
  .st-label { font-size:9px !important; }
  .stepbar { padding-bottom:14px !important; }
  .video-card { flex-direction:column !important; gap:10px !important; padding:14px !important; }
  .clip-header { flex-wrap:wrap !important; gap:8px !important; padding:10px 12px !important; }
  .time-tag { margin-left:0 !important; }
  .transcript-box { margin:0 10px 12px !important; font-size:11px !important; }
  .sched-row { flex-direction:column !important; align-items:flex-start !important; gap:4px !important; }
  .sched-title { margin:0 !important; }
  [data-testid="stHorizontalBlock"] { flex-wrap:wrap !important; }
  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
    min-width:100% !important; flex:1 1 100% !important;
  }
  div[data-testid="stButton"] > button { font-size:13px !important; }
  h1, h2 { font-size:20px !important; line-height:1.3 !important; }
  h3 { font-size:16px !important; }
}
</style>
""", unsafe_allow_html=True)

# ── セッション状態の保存・復元 ────────────────────────────
SESSION_FILE = OUTPUT_DIR / "session_state.json"

def _save_session(video_info: dict, clips: list):
    """解析結果をファイルに保存"""
    OUTPUT_DIR.mkdir(exist_ok=True)
    SESSION_FILE.write_text(
        __import__("json").dumps({"video_info": video_info, "clips": clips}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def _load_session() -> tuple[dict | None, list]:
    """保存済み解析結果を読み込む"""
    try:
        if SESSION_FILE.exists():
            data = __import__("json").loads(SESSION_FILE.read_text(encoding="utf-8"))
            video_info = data.get("video_info")
            # 旧バージョンで保存されたURLが破損している場合に備えてクリーニング
            if video_info and video_info.get("url"):
                from core.downloader import _clean_url
                video_info["url"] = _clean_url(video_info["url"])
            return video_info, data.get("clips", [])
    except Exception:
        pass
    return None, []

# ── セッション初期化 ───────────────────────────────────────
def _init():
    if "step" not in st.session_state:
        # 初回ロード時：保存済みセッションがあれば Step 2 から再開
        saved_info, saved_clips = _load_session()
        if saved_info and saved_clips:
            st.session_state["step"]       = 2
            st.session_state["video_info"] = saved_info
            st.session_state["clips"]      = saved_clips
        else:
            st.session_state["step"]       = 1
            st.session_state["video_info"] = None
            st.session_state["clips"]      = []

    defaults = {
        "schedule": {
            "start_date":    str((datetime.now() + timedelta(days=1)).date()),
            "start_time":    "10:00",
            "interval_hours": 24,
        },
        "results":  [],
        "running":  False,
        "tmp_dir":  None,
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
        if 1 <= _nav_step <= 4:
            s.step = _nav_step
    except Exception:
        pass
    st.query_params.clear()
    st.rerun()

# ── YouTube OAuth コールバック処理 ──
# ※ ログインチェック・render_login_page は全関数定義後（ファイル末尾）に実行


# ── ブランドヘッダー ──────────────────────────────────────
def render_logo():
    """アプリ上部にブランドヘッダー（ロゴ + サービス名 + ユーザー情報）を表示"""
    logo_path = BASE_DIR / "assets" / "logo.png"
    if logo_path.exists():
        logo_b64 = base64.b64encode(logo_path.read_bytes()).decode()
        logo_html = (
            f'<img src="data:image/png;base64,{logo_b64}"'
            f' class="brand-logo" alt="切り抜きくん"'
            f' onclick="window.top.location.href=\'/?nav=1\'"'
            f' style="cursor:pointer;" title="ホームに戻る">'
        )
    else:
        logo_html = (
            '<div class="brand-logo-fallback"'
            ' onclick="window.top.location.href=\'/?nav=1\'"'
            ' style="cursor:pointer;" title="ホームに戻る">✂️</div>'
        )

    # マルチユーザーモード: ユーザー情報表示
    user_section = ""
    if _is_multi_user_mode() and st.session_state.get("user_id"):
        email = st.session_state.get("user_email", "")
        # 先頭に空白・改行を入れると Markdown がコードブロックと誤認するため1行に
        user_section = f'<div class="header-user"><span class="header-user-email">{email[:28]}</span></div>'

    st.markdown(f"""
    <div class="app-header">
      {logo_html}
      <div class="brand-text">
        <div class="brand-catchcopy">動画の"おいしい瞬間"を切り抜くヒーロー</div>
        <div class="brand-name">切り抜きくん</div>
        <div class="brand-tagline">YouTube Shorts 自動作成ツール</div>
      </div>
      <div class="header-divider"></div>
      {user_section}
      <div class="header-badge">✂️ Beta</div>
    </div>
    """, unsafe_allow_html=True)

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
                    from core.auth import sign_out
                    sign_out()
                    for k in list(st.session_state.keys()):
                        del st.session_state[k]
                    st.rerun()
        else:
            with btn_cols[1]:
                if st.button("ログアウト", key="_logout_btn", help="ログアウトします"):
                    from core.auth import sign_out
                    sign_out()
                    for k in list(st.session_state.keys()):
                        del st.session_state[k]
                    st.rerun()


# ── ステップバー ──────────────────────────────────────────
def render_stepbar(current: int):
    render_logo()
    steps = [
        (1, "URL入力"),
        (2, "クリップ確認"),
        (3, "スケジュール"),
        (4, "実行"),
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

    # ── ユーザー一覧テーブル ──
    st.markdown("### 👥 ユーザー一覧")

    _PLAN_LABELS = {
        "free":     "🆓 無料",
        "lite":     "💡 ライト",
        "standard": "⭐ スタンダード",
        "pro":      "🚀 プロ",
    }

    # ヘッダー行
    hcols = st.columns([3, 1.5, 1, 1, 1, 1.5, 1.5])
    for col, label in zip(hcols, ["メール", "プラン", "今月使用", "上限", "YT接続", "登録日", "最終ログイン"]):
        col.markdown(f"**{label}**")
    st.markdown('<hr style="margin:4px 0;">', unsafe_allow_html=True)

    for user in users:
        row = st.columns([3, 1.5, 1, 1, 1, 1.5, 1.5])
        row[0].write(user["email"])
        row[1].write(_PLAN_LABELS.get(user["plan"], user["plan"]))
        row[2].write(str(user["clips_used"]))
        row[3].write(str(user["clips_limit"]))
        row[4].write("✅" if user["youtube_connected"] else "❌")
        row[5].write(user["created_at"])
        row[6].write(user["last_sign_in"])

    st.divider()

    # ── プラン変更 ──
    st.markdown("### ✏️ プラン変更")

    _PLAN_OPTIONS = ["free", "lite", "standard", "pro"]
    _PLAN_LIMITS  = {"free": 10, "lite": 30, "standard": 100, "pro": 9999}

    emails = [u["email"] for u in users]
    selected_email = st.selectbox("対象ユーザーを選択", emails, key="_admin_user_sel")
    selected_user  = next((u for u in users if u["email"] == selected_email), None)

    if selected_user:
        col_plan, col_btn = st.columns([3, 1])
        new_plan = col_plan.selectbox(
            "新しいプラン",
            _PLAN_OPTIONS,
            index=_PLAN_OPTIONS.index(selected_user.get("plan", "free")),
            format_func=lambda x: _PLAN_LABELS.get(x, x),
            key="_admin_plan_sel",
        )
        if col_btn.button("💾 変更", type="primary", key="_admin_plan_save"):
            update_user_plan(selected_user["id"], new_plan, _PLAN_LIMITS[new_plan])
            st.success(f"✅ {selected_email} → {_PLAN_LABELS[new_plan]} に変更しました")
            st.rerun()


# ── ログイン / 会員登録ページ ─────────────────────────────
def render_login_page():
    """マルチユーザーモード時のログイン・会員登録画面"""

    # ── Supabase メール確認: #access_token fragment を ?sb_access_token= に変換 ──
    # allow-same-origin iframe から window.top.document に <a> を注入してナビゲート
    import streamlit.components.v1 as _comp
    _comp.html("""
<script>
(function() {
  try {
    var hash = window.top.location.hash;
    if (!hash || hash.indexOf('access_token') === -1) return;
    var params = new URLSearchParams(hash.substring(1));
    var token   = params.get('access_token');
    var refresh = params.get('refresh_token') || '';
    var type    = params.get('type') || 'signup';
    if (!token) return;
    var loc = window.top.location;
    var url = loc.origin + loc.pathname
            + '?sb_access_token='   + encodeURIComponent(token)
            + '&sb_refresh_token='  + encodeURIComponent(refresh)
            + '&sb_type='           + encodeURIComponent(type);
    // <a> を parent document に注入して click → top-level ナビゲーション
    var a = window.top.document.createElement('a');
    a.href = url;
    window.top.document.body.appendChild(a);
    a.click();
    window.top.document.body.removeChild(a);
  } catch(e) { console.warn('sb-fragment-redirect:', e); }
})();
</script>
""", height=0)

    render_logo()

    # ※ OAuth メッセージは step4 で表示（ここでは不要）

    st.markdown("""
    <div style="max-width:440px;margin:40px auto;padding:0 20px;">
      <div style="font-size:22px;font-weight:800;color:#1e293b;margin-bottom:6px;text-align:center;">
        アカウントにログイン
      </div>
      <div style="font-size:13px;color:#64748b;margin-bottom:28px;text-align:center;">
        切り抜きくんを使うにはアカウントが必要です
      </div>
    </div>
    """, unsafe_allow_html=True)

    tab_login, tab_signup = st.tabs(["🔑 ログイン", "📝 会員登録"])

    # ─── ログインタブ ───────────────────────────────────────
    with tab_login:
        email_l = st.text_input("メールアドレス", key="login_email",
                                placeholder="you@example.com")
        pass_l  = st.text_input("パスワード", type="password", key="login_pass",
                                placeholder="••••••••")
        if st.button("ログイン", type="primary", use_container_width=True,
                     disabled=not (email_l.strip() and pass_l)):
            try:
                from core.auth import sign_in
                res = sign_in(email_l.strip(), pass_l)
                if res.session:
                    user = res.user
                    st.session_state["user_id"]    = user.id
                    st.session_state["user_email"] = user.email
                    # Supabase から YouTube トークンを取得
                    try:
                        from core.db import get_youtube_token
                        yt = get_youtube_token(user.id)
                        if yt:
                            st.session_state["yt_token"] = yt
                            # チャンネル情報も復元
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
                    st.rerun()
                else:
                    st.error("ログインに失敗しました。メールアドレスとパスワードを確認してください。")
            except Exception as e:
                st.error(f"ログインエラー: {e}")

    # ─── 会員登録タブ ───────────────────────────────────────
    with tab_signup:
        email_s = st.text_input("メールアドレス", key="signup_email",
                                placeholder="you@example.com")
        pass_s1 = st.text_input("パスワード（8文字以上）", type="password",
                                key="signup_pass1", placeholder="••••••••")
        pass_s2 = st.text_input("パスワード（確認）", type="password",
                                key="signup_pass2", placeholder="••••••••")

        pass_ok = len(pass_s1) >= 8 and pass_s1 == pass_s2

        if st.button("無料登録", type="primary", use_container_width=True,
                     disabled=not (email_s.strip() and pass_ok)):
            try:
                from core.auth import sign_up, sign_in
                res = sign_up(email_s.strip(), pass_s1)
                if res.user:
                    # ウェルカムメール送信（失敗しても登録は続行）
                    try:
                        from core.mailer import send_welcome_email
                        send_welcome_email(email_s.strip())
                    except Exception:
                        pass

                    # 確認メール不要設定の場合はそのままログイン
                    try:
                        login_res = sign_in(email_s.strip(), pass_s1)
                        if login_res.session:
                            st.session_state["user_id"]    = login_res.user.id
                            st.session_state["user_email"] = login_res.user.email
                            st.rerun()
                            return
                    except Exception:
                        pass
                    st.success("✅ 登録完了！確認メールを送信しました。メールを確認してからログインしてください。")
                else:
                    st.error("登録に失敗しました。")
            except Exception as e:
                err_str = str(e)
                if "already registered" in err_str.lower():
                    st.error("このメールアドレスは既に登録されています。ログインしてください。")
                elif "password" in err_str.lower():
                    st.error("パスワードが条件を満たしていません（8文字以上）。")
                else:
                    st.error(f"登録エラー: {e}")

        if pass_s1 and pass_s2 and not pass_ok:
            if len(pass_s1) < 8:
                st.caption("⚠️ パスワードは8文字以上で設定してください")
            elif pass_s1 != pass_s2:
                st.caption("⚠️ パスワードが一致しません")

    st.markdown("""
    <div style="text-align:center;font-size:11px;color:#94a3b8;margin-top:32px;">
      ✂️ 切り抜きくん Beta &nbsp;·&nbsp; 無料プランは月10本まで利用可能
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


# ══════════════════════════════════════════════════════════
# STEP 1 — URL 入力 & 解析
# ══════════════════════════════════════════════════════════
def step1():
    render_stepbar(1)
    st.markdown("""
    <div style="padding:28px 40px 0;margin-left:-40px;margin-right:-40px;">
      <div style="font-size:20px;font-weight:800;color:#1e293b;margin-bottom:4px;">
        🎬 元動画のURLを入力
      </div>
      <div style="font-size:13px;color:#64748b;margin-bottom:20px;">
        YouTube動画を1本入力するだけで、10本のShortsを自動で作成・予約投稿します。
      </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("")
    # 前回解析したURLをデフォルト表示
    _last_url = (s.video_info or {}).get("url", "")
    url = st.text_input(
        "YouTube URL",
        value=_last_url,
        placeholder="https://www.youtube.com/watch?v=xxxxxxxx",
        label_visibility="collapsed",
    )

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        clip_sec = st.slider("クリップの長さ（秒）", 20, 60, 58, key="clip_sec_s1")
    with col2:
        n_clips = st.number_input("本数", 1, 10, 10, key="n_clips_s1")
    with col3:
        st.markdown("")

    st.markdown("")
    if st.button("🔍 解析開始", type="primary", use_container_width=True,
                 disabled=not url.strip()):
        _analyze_error = None
        with st.status("動画を解析中...", expanded=True) as status:
            try:
                from core.analyzer import get_video_info, get_transcript, auto_select_clips

                st.write("📡 動画情報を取得しています...")
                info = get_video_info(url.strip())
                s.video_info = info
                st.write(f"✅ 動画タイトル: **{info['title'][:60]}**")
                st.write(f"⏱ 尺: {int(info['duration']//60)}分{int(info['duration']%60)}秒")

                st.write("📝 字幕・トランスクリプトを取得しています...")
                tmp = OUTPUT_DIR / "transcript"
                tmp.mkdir(parents=True, exist_ok=True)
                # 今回の動画と無関係な古いjson3を削除（別動画の字幕が混入しないよう）
                current_id = info.get("id", "")
                for old_f in tmp.glob("*.json3"):
                    if current_id and not old_f.name.startswith(current_id):
                        old_f.unlink(missing_ok=True)
                transcript = get_transcript(url.strip(), tmp)
                if transcript:
                    st.write(f"✅ 字幕取得完了（{len(transcript)} セグメント）")
                else:
                    st.write("⚠️ 字幕なし → 等間隔で自動分割します")

                st.write(f"✂️ {n_clips} 本のクリップを自動選定しています...")
                clips = auto_select_clips(
                    info["duration"], transcript,
                    n_clips=int(n_clips), clip_sec=clip_sec,
                    video_title=info.get("title", ""),
                )
                s.clips = clips
                st.write(f"✅ {len(clips)} 本のクリップを選定しました")

                _save_session(info, clips)
                status.update(label="解析完了！", state="complete")
                s.step = 2
                st.rerun()

            except Exception as e:
                status.update(label="エラーが発生しました", state="error")
                _analyze_error = e

        # st.status が折りたたまれてもエラーが見えるよう外に出す
        if _analyze_error is not None:
            st.error(f"❌ エラー: {_analyze_error}")


# ══════════════════════════════════════════════════════════
# STEP 2 — クリップ確認・編集
# ══════════════════════════════════════════════════════════

def _render_clip_preview(clip: dict, idx: int, video_id: str):
    """
    16:9 Shorts プレビューカード（テーマ・サイズ対応版）
    ┌─────────────────────────────┐
    │  ⚡ キャッチコピー（小）        │
    │  BIG TITLE テキスト           │  ← テーマグラデーション背景
    ├─────────────────────────────┤
    │     16:9 動画プレビュー        │  ← YouTube embed
    ├─────────────────────────────┤
    │     底部画像エリア             │  ← 顔写真・ロゴ等
    └─────────────────────────────┘
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
        size_key    = st.session_state.get("title_size",    "medium")
        pattern_key = st.session_state.get("title_pattern", "none")
    theme = TITLE_THEMES.get(theme_key, TITLE_THEMES["purple"])
    size  = TITLE_SIZES.get(size_key,   TITLE_SIZES["medium"])
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

    # 底部画像 HTML
    bottom_img_html = (
        '<div style="width:100%;height:100%;background:#f1f5f9;'
        'display:flex;align-items:center;justify-content:center;'
        'flex-direction:column;gap:4px;color:#94a3b8;">'
        '<span style="font-size:22px;">📷</span>'
        '<span style="font-size:10px;font-weight:600;">底部画像を設定</span>'
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

    # ── タイトルバー高さ動的計算（行数推定・日本語基準） ──
    _cpl  = {"small": 24, "medium": 19, "large": 14}[size_key]  # 1行あたり文字数
    _lh   = {"small": 17, "medium": 22, "large": 27}[size_key]  # 1行の高さ(px)
    _padv = {"small": 24, "medium": 32, "large": 38}[size_key]  # 上下パディング合計(px)
    _ch   = 26 if catchphrase else 0                             # キャッチコピー分(px)
    _lines = max(1, (len(title[:60]) + _cpl - 1) // _cpl)
    _dyn_h = _lines * _lh + _padv + _ch
    _title_bar_h = max(TITLE_BAR_H[size_key], _dyn_h)
    # カード全高 = タイトルバー + 動画(180) + 底部(90)
    card_h = _title_bar_h + 180 + 90

    card_html = f"""<!DOCTYPE html>
<html><head><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:transparent; font-family:-apple-system,'Hiragino Sans',sans-serif; }}
  .card {{
    width:320px; background:#fff;
    border-radius:16px; overflow:hidden;
    border:1px solid #cbd5e1;
    box-shadow:0 6px 28px rgba(0,0,0,0.16);
  }}
  .title-bar {{
    background:{theme["bg"]};
    padding:{size["pad"]};
    min-height:{_title_bar_h}px;
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
  }}
  .title-text {{
    color:{theme["text"]};
    font-size:{size["font"]}; font-weight:{size["weight"]};
    line-height:{size["lh"]}; letter-spacing:-0.01em;
    text-shadow:0 2px 8px rgba(0,0,0,0.40);
    word-break:break-all;
  }}
  .video-area {{
    width:320px; height:180px; background:#000; overflow:hidden;
  }}
  .video-area iframe {{ width:320px; height:180px; border:none; }}
  .bottom-area {{ width:320px; height:90px; overflow:hidden; }}
</style></head>
<body>
  <div class="card">
    <div class="title-bar">
      {catchphrase_html}
      <div class="title-text">{title[:60]}</div>
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
</body></html>"""
    components.html(card_html, height=card_h + 30, scrolling=False)

    # 底部画像 — 現在の設定サムネイル
    _bimg = clip.get("bottom_image")
    if _bimg and Path(_bimg).exists():
        th_l, th_r = st.columns([1, 2])
        with th_l:
            st.image(str(_bimg), use_container_width=True)
        with th_r:
            st.markdown(
                f'<div style="font-size:11px;color:#059669;font-weight:700;margin-top:4px;">'
                f'✅ 設定済み</div>'
                f'<div style="font-size:10px;color:#94a3b8;margin-top:2px;word-break:break-all;">'
                f'{Path(_bimg).name}</div>',
                unsafe_allow_html=True,
            )
            if st.button("🗑 削除", key=f"del_img_{idx}", use_container_width=True):
                Path(_bimg).unlink(missing_ok=True)
                clip["bottom_image"] = None
                _save_session(
                    st.session_state.get("video_info"),
                    st.session_state.get("clips", []),
                )
                st.rerun()
        st.markdown(
            '<div style="font-size:10px;color:#94a3b8;margin:4px 0 2px;">'
            '別の画像に変更:</div>', unsafe_allow_html=True
        )
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


def step2():
    render_stepbar(2)
    render_video_banner()

    st.markdown("""
    <div style="padding:28px 40px 0;margin-left:-40px;margin-right:-40px;">
      <div style="font-size:20px;font-weight:800;color:#1e293b;margin-bottom:4px;">
        ✂️ クリップを確認・編集
      </div>
      <div style="font-size:13px;color:#64748b;margin-bottom:20px;">
        自動選定された10本のクリップを確認してください。タイトル・説明・時間帯は自由に編集できます。
      </div>
    </div>
    """, unsafe_allow_html=True)

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

    # ── プロンプト適用ペンディング（widget 描画前に反映） ──────
    if "_design_pending" in st.session_state:
        _pd = st.session_state.pop("_design_pending")
        for _pk, _pv in _pd.items():
            st.session_state[_pk] = _pv
        if _pd.get("_clear_designs"):
            st.session_state.pop("clip_designs", None)

    # ── タイトルデザイン設定 UI ──────────────────────────────
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
            st.session_state.setdefault("title_size_sel",    "medium")
            st.session_state.setdefault("title_pattern_sel", "none")
            # 無効なキー値をリセット（パターン追加後の互換性確保）
            if st.session_state.get("title_theme_sel")   not in TITLE_THEMES:
                st.session_state["title_theme_sel"]   = "purple"
            if st.session_state.get("title_size_sel")    not in TITLE_SIZES:
                st.session_state["title_size_sel"]    = "medium"
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

        ap_col, cl_col = st.columns(2)
        with ap_col:
            if st.button(
                "✅ 全クリップに適用",
                key="bulk_apply_img",
                use_container_width=True,
                disabled=(bulk_up is None),
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

        # ── 左: 編集フォーム ／ 右: プレビュー ──
        edit_col, prev_col = st.columns([3, 2])

        with edit_col:
            # カードヘッダー（スコアバッジ付き）
            st.markdown(f"""
            <div class="clip-card">
              <div class="clip-header">
                <div class="clip-num">{clip['index']}</div>
                <div class="clip-title-preview">{"" if not clip['title'] else clip['title'][:40]}</div>
                <div style="display:flex;gap:6px;align-items:center;margin-left:auto;flex-wrap:wrap;">
                  <span title="スコア内訳&#10;📝文字密度:{s_density}/40&#10;🔥盛り上がり:{s_engage}/40&#10;✅文章完成度:{s_complete}/20"
                    style="background:{score_color};color:#fff;border-radius:7px;
                           padding:3px 9px;font-size:12px;font-weight:800;cursor:default;
                           box-shadow:0 1px 4px {score_color}66;">
                    ★ {score}点
                  </span>
                  <div class="time-tag">{time_str}</div>
                </div>
              </div>
            </div>
            """, unsafe_allow_html=True)

            # ℹ️ 採点根拠ポップオーバー
            _pc, _ = st.columns([2.2, 3.8])
            with _pc:
                with st.popover("ℹ️ 採点根拠", use_container_width=True):
                    st.markdown(f"#### ★ 合計 **{score}** / 100点")
                    c1, c2, c3 = st.columns(3)
                    c1.metric("📝 文字密度",   f"{s_density} / 40")
                    c2.metric("🔥 盛り上がり", f"{s_engage} / 40")
                    c3.metric("✅ 完成度",     f"{s_complete} / 20")
                    st.markdown(
                        "<small>"
                        "📝 **文字密度** — 発話量（文字数/秒）<br>"
                        "🔥 **盛り上がり** — ？！・すごい・秘密 等のキーワード数<br>"
                        "✅ **完成度** — 字幕セグメントの充実度"
                        "</small>",
                        unsafe_allow_html=True,
                    )

            # 字幕プレビュー
            if clip.get("transcript"):
                st.markdown(
                    f'<div class="transcript-box">{clip["transcript"][:180]}...</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<div class="transcript-box no-transcript">（字幕なし）</div>',
                    unsafe_allow_html=True,
                )

            # 編集フォーム
            r1, r2, r3, r4 = st.columns([1, 1, 1, 0.5])
            with r1:
                clip["title"] = st.text_input(
                    "📝 タイトル", value=clip.get("title", ""),
                    key=f"title_{i}", placeholder="Shorts タイトル（〜40文字）",
                )
            with r2:
                clip["hashtags"] = st.text_input(
                    "＃ ハッシュタグ", value=clip.get("hashtags", "#Shorts"),
                    key=f"tags_{i}", placeholder="#AI活用 #Shorts",
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
                    "⚡ キャッチコピー（タイトル上部）",
                    value=clip.get("catchphrase", ""),
                    key=f"catch_{i}",
                    placeholder="知らないと損！👀",
                    max_chars=25,
                    help="動画プレビューのタイトルエリア上部に表示される短いフレーズ（〜25文字）",
                )
            with desc_col:
                clip["description"] = st.text_area(
                    "説明文", value=clip.get("description", ""),
                    key=f"desc_{i}", height=68, placeholder="説明文（省略可）",
                )

        with prev_col:
            _render_clip_preview(clip, i, video_id)

        st.markdown('<hr style="border-color:#e5e7eb;margin:8px 0;">', unsafe_allow_html=True)

    s.clips = clips
    _save_session(s.video_info, clips)

    # ナビゲーション
    col_back, col_next = st.columns([1, 3])
    with col_back:
        if st.button("🔄 新しい動画", key="back2"):
            SESSION_FILE.unlink(missing_ok=True)
            for k in ["step", "video_info", "clips", "results"]:
                del st.session_state[k]
            st.rerun()
    with col_next:
        enabled_count = sum(1 for c in clips if c.get("enabled", True))
        if st.button(
            f"スケジュール設定へ →（{enabled_count}本）",
            type="primary", use_container_width=True,
            disabled=enabled_count == 0,
        ):
            s.step = 3
            st.rerun()


# ══════════════════════════════════════════════════════════
# STEP 3 — スケジュール設定
# ══════════════════════════════════════════════════════════
def step3():
    render_stepbar(3)
    render_video_banner()

    st.markdown("""
    <div style="padding:28px 40px 0;margin-left:-40px;margin-right:-40px;">
      <div style="font-size:20px;font-weight:800;color:#1e293b;margin-bottom:4px;">
        ⏰ 投稿スケジュールを設定
      </div>
    </div>
    """, unsafe_allow_html=True)

    sched = s.schedule
    col1, col2 = st.columns(2)
    with col1:
        from datetime import date as dt_date, time as dt_time
        try:
            init_date = datetime.strptime(sched["start_date"], "%Y-%m-%d").date()
        except Exception:
            init_date = (datetime.now() + timedelta(days=1)).date()
        start_date = st.date_input("初回投稿日（JST）", value=init_date)

        try:
            init_time = datetime.strptime(sched["start_time"], "%H:%M").time()
        except Exception:
            init_time = dt_time(10, 0)
        start_time = st.time_input("初回投稿時刻（JST）", value=init_time)

    with col2:
        interval_h = st.number_input(
            "投稿間隔（時間）", min_value=1, max_value=720,
            value=int(sched.get("interval_hours", 24)),
        )
        category = st.selectbox(
            "カテゴリー",
            options=["22 - 人・ブログ", "27 - 教育", "28 - 科学と技術",
                     "24 - エンターテインメント", "26 - ハウツー・スタイル"],
            index=0,
        )
        sched["category_id"] = category.split(" ")[0]

    sched["start_date"]     = str(start_date)
    sched["start_time"]     = start_time.strftime("%H:%M")
    sched["interval_hours"] = int(interval_h)

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

    st.info(
        "ℹ️ **関連動画**の手動設定はYouTube APIで廃止済みです。"
        "動画の説明欄・エンドカード・固定コメントで関連動画へ誘導してください。",
        icon=None,
    )

    # プレビュー
    st.markdown("")
    st.markdown("### 📅 投稿スケジュール プレビュー")

    enabled_clips = [c for c in s.clips if c.get("enabled", True)]
    try:
        base_dt = datetime.strptime(
            f"{sched['start_date']} {sched['start_time']}", "%Y-%m-%d %H:%M"
        )
        for i, clip in enumerate(enabled_clips):
            post_dt = base_dt + timedelta(hours=i * int(interval_h))
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
        if st.button("← 戻る", key="back3"):
            s.step = 2
            st.rerun()
    with col_next:
        if st.button("実行画面へ →", type="primary", use_container_width=True):
            s.schedule = sched
            s.step = 4
            st.rerun()


# ══════════════════════════════════════════════════════════
# STEP 4 — 実行
# ══════════════════════════════════════════════════════════
def step4():
    render_stepbar(4)
    render_video_banner()

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
    secret_ok  = (CREDS_DIR / "client_secret.json").exists()
    multi_mode = _is_multi_user_mode()

    if multi_mode:
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
                            _user_id      = s.get("user_id", "anon")
                            _code_verifier = _sec.token_urlsafe(96)  # PKCE code_verifier
                            _state        = _make_oauth_state(_user_id, _code_verifier)
                            _auth_url, _  = _gau(_get_app_url(), state=_state, code_verifier=_code_verifier)
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

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("処理本数",   f"{len(enabled_clips)} 本")
    col2.metric("元動画",     (s.video_info or {}).get("title", "—")[:20])
    try:
        base_dt = datetime.strptime(
            f"{sched['start_date']} {sched['start_time']}", "%Y-%m-%d %H:%M"
        )
        last_dt = base_dt + timedelta(hours=(len(enabled_clips)-1) * int(sched["interval_hours"]))
        col3.metric("初回投稿", base_dt.strftime("%m/%d %H:%M"))
        col4.metric("最終投稿", last_dt.strftime("%m/%d %H:%M"))
    except Exception:
        pass

    st.markdown("")

    # ── 実行ボタン ──
    all_ready = secret_ok and token_ok and len(enabled_clips) > 0
    if not all_ready:
        st.warning("YouTube認証を完了してから実行してください")

    col_back, col_run = st.columns([1, 3])
    with col_back:
        if st.button("← 戻る", key="back4", disabled=s.running):
            s.step = 3
            st.rerun()
    with col_run:
        if st.button(
            f"▶️  {len(enabled_clips)} 本のShortsを作成・予約投稿",
            type="primary", use_container_width=True,
            disabled=(not all_ready or s.running),
        ):
            s.running = True
            _run_pipeline(enabled_clips, sched)
            s.running = False

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
            for k in ["step","video_info","clips","results","running"]:
                del st.session_state[k]
            st.rerun()


# ── パイプライン実行 ──────────────────────────────────────
def _run_pipeline(clips: list, sched: dict):
    from core.downloader import download_video
    from core.processor  import create_shorts
    from core.uploader   import upload_shorts

    video_info = s.video_info
    interval_h = int(sched["interval_hours"])
    category   = sched.get("category_id", "22")

    try:
        base_dt = datetime.strptime(
            f"{sched['start_date']} {sched['start_time']}", "%Y-%m-%d %H:%M"
        )
    except Exception:
        st.error("スケジュール日時が正しくありません")
        return

    # ── マルチユーザー: YouTube トークン取得＆リフレッシュ ──
    _yt_token  = None
    _user_id   = None
    if _is_multi_user_mode():
        _user_id  = s.get("user_id")
        _yt_token = s.get("yt_token")
        if not _yt_token:
            st.error("YouTubeチャンネルが接続されていません。認証セクションで接続してください。")
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
            st.error(f"YouTubeトークンのリフレッシュに失敗しました。再接続してください。({_e})")
            return

    results = []
    OUTPUT_DIR.mkdir(exist_ok=True)

    with st.status("処理中...", expanded=True) as status:
        prog = st.progress(0, text="準備中...")

        # ① 元動画を1回だけダウンロード
        st.write(f"⬇️ 元動画をダウンロード中: `{video_info['url'][:60]}`")
        try:
            raw_path = download_video(video_info["url"], OUTPUT_DIR / "raw")
            st.write(f"✅ ダウンロード完了: `{raw_path.name}`")
        except Exception as e:
            st.error(f"❌ ダウンロード失敗: {e}")
            status.update(label="ダウンロード失敗", state="error")
            return

        # ② 各クリップを処理
        for i, clip in enumerate(clips):
            pct  = (i + 1) / len(clips)
            title = clip["title"] or f"Shorts {clip['index']}"
            hashtags = clip.get("hashtags", "#Shorts")
            description = (clip.get("description","").strip() + "\n\n" + hashtags).strip()
            tags = [t.lstrip("#") for t in hashtags.split() if t.startswith("#")]

            jst_dt = base_dt + timedelta(hours=i * interval_h)
            utc_dt = (jst_dt - timedelta(hours=9)).replace(tzinfo=timezone.utc)

            prog.progress(pct, text=f"[{i+1}/{len(clips)}] {title[:40]}")

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
                    _size_key    = st.session_state.get("title_size",    "medium")
                    _pattern_key = st.session_state.get("title_pattern", "none")

                _bottom_img = clip.get("bottom_image")
                _bottom_path = Path(_bottom_img) if _bottom_img else None

                # 変換
                st.write(f"✂️ **{i+1}本目: 切り出し変換中** "
                         f"({int(clip['start'])}s → {int(clip['end'])}s)")
                shorts_path = OUTPUT_DIR / "shorts" / f"short_{clip['index']:02d}.mp4"
                create_shorts(
                    raw_path, shorts_path,
                    max_duration=int(clip["end"] - clip["start"]),
                    start_sec=int(clip["start"]),
                    title=title,
                    theme_key=_theme_key,
                    size_key=_size_key,
                    pattern_key=_pattern_key,
                    themes=TITLE_THEMES,
                    sizes=TITLE_SIZES,
                    bottom_image_path=_bottom_path,
                    catchphrase=clip.get("catchphrase", ""),
                )

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

                results.append({
                    "num": i+1, "title": title, "video_id": video_id,
                    "publish_jst": jst_dt.strftime("%Y/%m/%d %H:%M"), "status": "✅"
                })
                st.write(
                    f"✅ **完了** → "
                    f"[youtube.com/shorts/{video_id}](https://youtube.com/shorts/{video_id})"
                )

            except Exception as e:
                results.append({
                    "num": i+1, "title": title, "video_id": None,
                    "publish_jst": jst_dt.strftime("%Y/%m/%d %H:%M"), "status": f"❌ {e}"
                })
                st.write(f"❌ **エラー [{i+1}本目]**: {e}")

        prog.progress(1.0, text="全処理完了！")
        ok = sum(1 for r in results if r["video_id"])
        status.update(
            label=f"🎉 完了！{ok}/{len(results)} 本の予約投稿が完了しました",
            state="complete",
        )

        # マルチユーザー: 使用量を更新
        if _is_multi_user_mode() and _user_id and ok > 0:
            try:
                from core.db import increment_clips_used
                increment_clips_used(_user_id, ok)
            except Exception:
                pass

    s.results = results


# ══════════════════════════════════════════════════════════
# ルーティング（全関数定義後に実行）
# ══════════════════════════════════════════════════════════

# ── YouTube OAuth コールバック処理 ──
if "code" in st.query_params:
    _handle_oauth_callback()

# ── Supabase メール確認トークン処理 ──
if "sb_access_token" in st.query_params:
    _handle_supabase_confirmation()

# ── マルチユーザー: ログインチェック ──
if _is_multi_user_mode():
    if not s.get("user_id"):
        render_login_page()
        st.stop()

# ── 管理者パネル ──
if st.query_params.get("page") == "admin":
    if _is_admin():
        render_admin_panel()
        st.stop()
    else:
        # 非管理者はメインページへリダイレクト
        st.query_params.clear()
        st.rerun()

# ── メール認証完了メッセージ ──
if st.session_state.pop("_email_confirmed", False):
    st.success("✅ メールアドレスを確認しました。ようこそ切り抜きくんへ！")

STEPS = {1: step1, 2: step2, 3: step3, 4: step4}
STEPS[s.step]()

st.markdown(
    '<div class="footer">✂️ 切り抜きくん &nbsp;·&nbsp; '
    'Powered by yt-dlp / ffmpeg / YouTube Data API v3</div>',
    unsafe_allow_html=True,
)
