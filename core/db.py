"""
Supabase DB 操作
- YouTube トークン（ユーザーごと）
- サブスクリプション / 使用量管理
- 管理者用ユーザー一覧・プラン変更

NOTE: get_supabase_admin() を使用することで RLS をバイパスし、
      サーバーサイドから安全に操作する。
"""
import json


# ─────────────────────────────────────────────
# YouTube トークン
# ─────────────────────────────────────────────

def get_youtube_token(user_id: str) -> dict | None:
    """ユーザーの YouTube トークンを取得。なければ None を返す"""
    try:
        from core.auth import get_supabase_admin
        sb = get_supabase_admin()
        res = (
            sb.table("youtube_tokens")
            .select("token_json")
            .eq("user_id", user_id)
            .execute()
        )
        if res.data:
            return json.loads(res.data[0]["token_json"])
    except Exception:
        pass
    return None


def save_youtube_token(user_id: str, token):
    """
    YouTube トークンを保存 / 更新。
    token は Credentials オブジェクト、JSON 文字列、または辞書を受け付ける。
    """
    try:
        from core.auth import get_supabase_admin
        if hasattr(token, "to_json"):
            token_str = token.to_json()
        elif isinstance(token, dict):
            token_str = json.dumps(token)
        else:
            token_str = str(token)

        sb = get_supabase_admin()
        sb.table("youtube_tokens").upsert(
            {"user_id": user_id, "token_json": token_str},
            on_conflict="user_id",
        ).execute()
    except Exception as e:
        raise RuntimeError(f"YouTubeトークンの保存に失敗しました: {e}") from e


def delete_youtube_token(user_id: str):
    """YouTube トークンを削除"""
    try:
        from core.auth import get_supabase_admin
        sb = get_supabase_admin()
        sb.table("youtube_tokens").delete().eq("user_id", user_id).execute()
    except Exception:
        pass


def submit_youtube_request(user_id: str, google_email: str):
    """YouTube接続申請を保存（Googleアカウントメールを subscriptions に記録）"""
    try:
        from core.auth import get_supabase_admin
        sb = get_supabase_admin()
        sb.table("subscriptions").upsert(
            {"user_id": user_id, "youtube_request_email": google_email},
            on_conflict="user_id",
        ).execute()
    except Exception as e:
        raise RuntimeError(f"申請の保存に失敗しました: {e}") from e


def set_youtube_approved(user_id: str, approved: bool = True):
    """YouTube接続承認フラグを設定（管理者専用）"""
    from core.auth import get_supabase_admin
    sb = get_supabase_admin()
    sb.table("subscriptions").upsert(
        {"user_id": user_id, "youtube_approved": approved},
        on_conflict="user_id",
    ).execute()


# ─────────────────────────────────────────────
# サブスクリプション / 使用量
# ─────────────────────────────────────────────

_FREE_PLAN = {
    "plan": "free",
    "clips_limit": 10,
    "clips_used_this_month": 0,
    "status": "active",
    "stripe_customer_id": None,
    "stripe_subscription_id": None,
}


def get_subscription(user_id: str) -> dict:
    """ユーザーのサブスク情報を取得。なければ無料プランのデフォルトを返す"""
    try:
        from core.auth import get_supabase_admin
        sb = get_supabase_admin()
        res = (
            sb.table("subscriptions")
            .select("*")
            .eq("user_id", user_id)
            .execute()
        )
        if res.data:
            return res.data[0]
    except Exception:
        pass
    return dict(_FREE_PLAN)


def get_clips_remaining(user_id: str) -> int:
    """今月の残りクリップ本数を返す"""
    sub = get_subscription(user_id)
    limit = sub.get("clips_limit") or 10
    used  = sub.get("clips_used_this_month") or 0
    return max(0, limit - used)


def increment_clips_used(user_id: str, count: int = 1):
    """今月の使用クリップ数を加算"""
    try:
        from core.auth import get_supabase_admin
        sub = get_subscription(user_id)
        new_count = (sub.get("clips_used_this_month") or 0) + count
        sb = get_supabase_admin()
        sb.table("subscriptions").update(
            {"clips_used_this_month": new_count}
        ).eq("user_id", user_id).execute()
    except Exception:
        pass


def get_plan_label(plan: str) -> str:
    """プランキーを表示名に変換"""
    return {
        "free":       "🆓 無料プラン（月10本）",
        "lite":       "💡 ライトプラン（月30本）",
        "standard":   "⭐ スタンダードプラン（月100本）",
        "pro":        "🚀 プロプラン（月無制限）",
    }.get(plan, f"プラン: {plan}")


# ─────────────────────────────────────────────
# 管理者用
# ─────────────────────────────────────────────

def get_all_users_with_stats() -> list:
    """
    全ユーザーの統計情報を取得（管理者専用）。
    auth.users + subscriptions + youtube_tokens を結合して返す。
    """
    from core.auth import get_supabase_admin
    sb = get_supabase_admin()

    # サブスクリプション一覧
    subs_res = sb.table("subscriptions").select("*").execute()
    subs_by_uid = {row["user_id"]: row for row in (subs_res.data or [])}

    # YouTube 接続済み UID セット
    tokens_res = sb.table("youtube_tokens").select("user_id").execute()
    token_uids = {row["user_id"] for row in (tokens_res.data or [])}

    # Auth ユーザー一覧（service_role 必須）
    auth_res = sb.auth.admin.list_users()
    # supabase-py v2 はリスト直接 or .users 属性のどちらかを返す
    auth_users = auth_res if isinstance(auth_res, list) else getattr(auth_res, "users", [])

    result = []
    for user in auth_users:
        uid          = getattr(user, "id", None) or user.get("id", "")
        email        = getattr(user, "email", None) or user.get("email", "—")
        created_at   = getattr(user, "created_at", None) or user.get("created_at", "")
        last_sign_in = getattr(user, "last_sign_in_at", None) or user.get("last_sign_in_at", "")
        confirmed    = getattr(user, "email_confirmed_at", None) or user.get("email_confirmed_at")

        sub = subs_by_uid.get(uid, {})
        result.append({
            "id":               uid,
            "email":            email or "—",
            "created_at":       str(created_at)[:10]   if created_at   else "—",
            "last_sign_in":     str(last_sign_in)[:10] if last_sign_in else "—",
            "email_confirmed":  bool(confirmed),
            "plan":             sub.get("plan", "free"),
            "clips_limit":      sub.get("clips_limit", 10),
            "clips_used":       sub.get("clips_used_this_month", 0),
            "youtube_connected":      uid in token_uids,
            "youtube_approved":       bool(sub.get("youtube_approved", False)),
            "youtube_request_email":  sub.get("youtube_request_email") or "",
        })

    # 登録日降順
    result.sort(key=lambda u: u["created_at"], reverse=True)
    return result


def update_user_plan(user_id: str, plan: str, clips_limit: int):
    """ユーザーのプランを変更（管理者専用）"""
    from core.auth import get_supabase_admin
    sb = get_supabase_admin()
    sb.table("subscriptions").upsert(
        {"user_id": user_id, "plan": plan, "clips_limit": clips_limit},
        on_conflict="user_id",
    ).execute()


def delete_user(user_id: str) -> None:
    """
    ユーザーを完全削除（管理者専用）。
    - youtube_tokens テーブルの行を削除
    - subscriptions テーブルの行を削除
    - Supabase Auth からユーザーアカウントを削除
    いずれかが失敗した場合は RuntimeError を送出。
    """
    from core.auth import get_supabase_admin
    sb = get_supabase_admin()

    # 関連データを先に削除（FK 制約がある場合に備える）
    try:
        sb.table("youtube_tokens").delete().eq("user_id", user_id).execute()
    except Exception as e:
        raise RuntimeError(f"YouTubeトークンの削除に失敗: {e}") from e

    try:
        sb.table("subscriptions").delete().eq("user_id", user_id).execute()
    except Exception as e:
        raise RuntimeError(f"サブスクリプションの削除に失敗: {e}") from e

    # Supabase Auth からアカウント削除（service_role 必須）
    try:
        sb.auth.admin.delete_user(user_id)
    except Exception as e:
        raise RuntimeError(f"Authユーザーの削除に失敗: {e}") from e
