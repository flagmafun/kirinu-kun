"""
Railway 起動スクリプト
Railway の環境変数 → ~/.streamlit/secrets.toml を生成してから Streamlit を起動する
"""
import json
import os
from pathlib import Path


def _q(s: str) -> str:
    """文字列を TOML 用にダブルクォート & エスケープ（json.dumps と同じ形式）"""
    return json.dumps(s)


def main() -> None:
    # ── secrets.toml 生成 ──────────────────────────────────────────────
    secrets_dir = Path.home() / ".streamlit"
    secrets_dir.mkdir(parents=True, exist_ok=True)

    supabase_url              = os.environ.get("SUPABASE_URL", "")
    supabase_anon_key         = os.environ.get("SUPABASE_ANON_KEY", "")
    supabase_service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    youtube_client_secret     = os.environ.get("YOUTUBE_CLIENT_SECRET_JSON", "")
    app_url                   = os.environ.get("APP_URL", "")
    admin_emails_raw          = os.environ.get("APP_ADMIN_EMAILS", "[]")

    # APP_ADMIN_EMAILS は JSON 配列文字列 '["a@b.com","c@d.com"]'
    # またはカンマ区切り "a@b.com,c@d.com" のどちらでも受け付ける
    try:
        admin_emails = json.loads(admin_emails_raw)
        if not isinstance(admin_emails, list):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        admin_emails = [e.strip() for e in admin_emails_raw.split(",") if e.strip()]

    toml_content = f"""\
[supabase]
url = {_q(supabase_url)}
anon_key = {_q(supabase_anon_key)}
service_role_key = {_q(supabase_service_role_key)}

[youtube]
client_secret_json = {_q(youtube_client_secret)}

[app]
url = {_q(app_url)}
admin_emails = {json.dumps(admin_emails)}
"""
    (secrets_dir / "secrets.toml").write_text(toml_content)
    print("✅ secrets.toml を生成しました")

    # ── Streamlit 起動 ─────────────────────────────────────────────────
    port = os.environ.get("PORT", "8501")
    os.execvp("streamlit", [
        "streamlit", "run", "app.py",
        "--server.port", port,
        "--server.address", "0.0.0.0",
        "--server.headless", "true",
        "--server.enableCORS", "false",
        "--server.enableXsrfProtection", "false",
    ])


if __name__ == "__main__":
    main()
