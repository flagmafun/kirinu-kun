"""
Supabase DB 操作
- YouTube トークン（ユーザーごと）
- サブスクリプション / 使用量管理
"""
import json


# ─────────────────────────────────────────────
# YouTube トークン
# ─────────────────────────────────────────────

def get_youtube_token(user_id: str) -> dict | None:
    """ユーザーの YouTube トークンを取得。なければ None を返す"""
    try:
        from core.auth import get_supabase
        sb = get_supabase()
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
        from core.auth import get_supabase
        if hasattr(token, "to_json"):
            token_str = token.to_json()
        elif isinstance(token, dict):
            token_str = json.dumps(token)
        else:
            token_str = str(token)

        sb = get_supabase()
        sb.table("youtube_tokens").upsert(
            {"user_id": user_id, "token_json": token_str},
            on_conflict="user_id",
        ).execute()
    except Exception as e:
        raise RuntimeError(f"YouTubeトークンの保存に失敗しました: {e}") from e


def delete_youtube_token(user_id: str):
    """YouTube トークンを削除"""
    try:
        from core.auth import get_supabase
        sb = get_supabase()
        sb.table("youtube_tokens").delete().eq("user_id", user_id).execute()
    except Exception:
        pass


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
        from core.auth import get_supabase
        sb = get_supabase()
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
        from core.auth import get_supabase
        sub = get_subscription(user_id)
        new_count = (sub.get("clips_used_this_month") or 0) + count
        sb = get_supabase()
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
