"""
メール送信ヘルパー
Resend API（https://resend.com / 無料: 3000通/月）を使用。
RESEND_API_KEY が未設定の場合は送信をスキップする（エラーにしない）。
"""
import os
import json


def _get_resend_key() -> str | None:
    try:
        import streamlit as st
        return st.secrets["email"].get("resend_api_key", "") or None
    except Exception:
        return os.environ.get("RESEND_API_KEY", "") or None


def _get_from_address() -> str:
    try:
        import streamlit as st
        return st.secrets["email"].get("from_address", "切り抜きくん <noreply@resend.dev>")
    except Exception:
        return os.environ.get("EMAIL_FROM", "切り抜きくん <noreply@resend.dev>")


def _get_app_url() -> str:
    try:
        import streamlit as st
        return st.secrets["app"]["url"]
    except Exception:
        return os.environ.get("APP_URL", "https://kirinu-kun.streamlit.app")


def send_welcome_email(to_email: str) -> bool:
    """
    新規登録ウェルカムメールを送信する。
    RESEND_API_KEY 未設定の場合は何もせず True を返す（必須ではないため）。
    """
    api_key = _get_resend_key()
    if not api_key:
        return True  # メール設定なし → スキップ（エラーにしない）

    app_url = _get_app_url()

    html_body = f"""
<!DOCTYPE html>
<html lang="ja">
<body style="font-family:'Helvetica Neue',Arial,sans-serif;background:#f8fafc;margin:0;padding:0;">
<div style="max-width:560px;margin:40px auto;background:#ffffff;border-radius:16px;
            box-shadow:0 2px 12px rgba(0,0,0,.08);overflow:hidden;">

  <!-- ヘッダー -->
  <div style="background:linear-gradient(135deg,#7c3aed,#4f46e5);padding:36px 40px;text-align:center;">
    <div style="font-size:36px;">✂️</div>
    <div style="color:#fff;font-size:22px;font-weight:800;margin-top:8px;">切り抜きくん</div>
    <div style="color:#ddd6fe;font-size:13px;margin-top:4px;">YouTube Shorts 自動作成ツール</div>
  </div>

  <!-- 本文 -->
  <div style="padding:36px 40px;">
    <h2 style="color:#1e293b;font-size:20px;margin:0 0 16px;">ご登録ありがとうございます！</h2>
    <p style="color:#475569;font-size:14px;line-height:1.7;margin:0 0 24px;">
      切り抜きくんへようこそ。<br>
      あなたのアカウントが作成されました。
    </p>

    <!-- アカウント情報 -->
    <div style="background:#f1f5f9;border-radius:12px;padding:20px 24px;margin-bottom:28px;">
      <div style="font-size:12px;color:#64748b;font-weight:600;margin-bottom:12px;
                  text-transform:uppercase;letter-spacing:.05em;">アカウント情報</div>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <tr>
          <td style="color:#64748b;padding:6px 0;width:120px;">メールアドレス</td>
          <td style="color:#1e293b;font-weight:600;">{to_email}</td>
        </tr>
        <tr>
          <td style="color:#64748b;padding:6px 0;">プラン</td>
          <td style="color:#1e293b;font-weight:600;">🆓 無料プラン（月10本）</td>
        </tr>
        <tr>
          <td style="color:#64748b;padding:6px 0;">ログインURL</td>
          <td><a href="{app_url}" style="color:#7c3aed;">{app_url}</a></td>
        </tr>
      </table>
    </div>

    <!-- CTAボタン -->
    <div style="text-align:center;margin-bottom:28px;">
      <a href="{app_url}"
         style="display:inline-block;background:#7c3aed;color:#fff;
                font-size:15px;font-weight:700;padding:14px 40px;
                border-radius:10px;text-decoration:none;">
        今すぐ使ってみる
      </a>
    </div>

    <p style="color:#94a3b8;font-size:12px;text-align:center;margin:0;">
      ご不明な点はこのメールに返信してください。<br>
      ✂️ 切り抜きくん運営チーム
    </p>
  </div>
</div>
</body>
</html>
"""

    try:
        import urllib.request
        data = json.dumps({
            "from":    _get_from_address(),
            "to":      [to_email],
            "subject": "【切り抜きくん】ご登録ありがとうございます",
            "html":    html_body,
        }).encode()

        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False  # メール失敗は致命的エラーにしない
