"""
使用量・プラン管理
- subscriptions テーブルの clips_limit / clips_used_this_month を参照
- プラン定義:
    trial  : 5本（無料プレゼント）
    basic  : 105本/月（100本 + 無料5本）
    pro    : 505本/月（500本 + 無料5本）
    test   : 無制限（テストユーザー）
"""
from __future__ import annotations

# プラン定義
PLANS: dict[str, dict] = {
    "trial": {"label": "🆓 無料トライアル（5本）",   "limit": 5,   "price": 0},
    "basic": {"label": "⭐ ベーシック（月105本）",    "limit": 105, "price": 50000},
    "pro":   {"label": "🚀 プロ（月505本）",          "limit": 505, "price": 200000},
    "test":  {"label": "🔧 テストユーザー（無制限）", "limit": 999999, "price": 0},
}

# Stripe Price ID（Railway 環境変数から取得）
import os
STRIPE_PRICE_BASIC = os.environ.get("STRIPE_PRICE_BASIC", "")
STRIPE_PRICE_PRO   = os.environ.get("STRIPE_PRICE_PRO",   "")


def get_plan_info(user_id: str) -> dict:
    """
    ユーザーのプラン情報を返す。
    {plan, label, limit, used, remaining, is_test, subscription_status, current_period_end}
    """
    from core.db import get_subscription
    sub   = get_subscription(user_id)
    plan  = sub.get("plan", "trial")
    is_test = (plan == "test")
    limit = PLANS[plan]["limit"] if plan in PLANS else sub.get("clips_limit", 5)
    used  = sub.get("clips_used_this_month", 0) if not is_test else 0
    remaining = 999999 if is_test else max(0, limit - used)
    return {
        "plan":                plan,
        "label":               PLANS.get(plan, {}).get("label", plan),
        "limit":               limit,
        "used":                used,
        "remaining":           remaining,
        "is_test":             is_test,
        "subscription_status": sub.get("subscription_status", "active"),
        "current_period_end":  sub.get("current_period_end"),
    }


def check_can_generate(user_id: str, clips_count: int = 1) -> tuple[bool, str]:
    """
    生成可能かチェック。
    戻り値: (ok: bool, error_message: str)
    """
    info = get_plan_info(user_id)
    if info["is_test"]:
        return True, ""
    remaining = info["remaining"]
    if remaining < clips_count:
        plan_label = info["label"]
        return False, (
            f"生成枠が不足しています "
            f"（あと {remaining} 本 / 必要 {clips_count} 本）。\n"
            f"現在のプラン: {plan_label}"
        )
    return True, ""


def increment_usage(user_id: str, count: int = 1):
    """使用本数を加算する（テストユーザーはスキップ）"""
    info = get_plan_info(user_id)
    if info["is_test"]:
        return
    from core.db import increment_clips_used
    increment_clips_used(user_id, count)
