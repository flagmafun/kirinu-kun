"""
使用量・プラン管理
- subscriptions テーブルの clips_limit / clips_used_this_month を参照
- プラン定義: trial(5本) / basic(100本) / pro(500本)
"""
from __future__ import annotations

# プラン定義
PLANS: dict[str, dict] = {
    "trial": {"label": "無料トライアル", "limit": 5,   "price": 0},
    "basic": {"label": "ベーシック",      "limit": 100, "price": 50000},
    "pro":   {"label": "プロ",            "limit": 500, "price": 200000},
}

# Stripe Price ID（Railway 環境変数から取得）
import os
STRIPE_PRICE_BASIC = os.environ.get("STRIPE_PRICE_BASIC", "")
STRIPE_PRICE_PRO   = os.environ.get("STRIPE_PRICE_PRO",   "")


def get_plan_info(user_id: str) -> dict:
    """
    ユーザーのプラン情報を返す。
    {plan, label, limit, used, remaining, subscription_status, current_period_end}
    """
    from core.db import get_subscription
    sub   = get_subscription(user_id)
    plan  = sub.get("plan", "trial")
    limit = sub.get("clips_limit", PLANS.get(plan, PLANS["trial"])["limit"])
    used  = sub.get("clips_used_this_month", 0)
    return {
        "plan":                plan,
        "label":               PLANS.get(plan, {}).get("label", plan),
        "limit":               limit,
        "used":                used,
        "remaining":           max(0, limit - used),
        "subscription_status": sub.get("subscription_status", "active"),
        "current_period_end":  sub.get("current_period_end"),
    }


def check_can_generate(user_id: str, clips_count: int = 1) -> tuple[bool, str]:
    """
    生成可能かチェック。
    戻り値: (ok: bool, error_message: str)
    """
    info      = get_plan_info(user_id)
    remaining = info["remaining"]
    if remaining < clips_count:
        plan_label = info["label"]
        return False, (
            f"今月の残り生成枠が不足しています "
            f"（残り {remaining} 本 / 必要 {clips_count} 本）。\n"
            f"現在のプラン: {plan_label}（月 {info['limit']} 本）"
        )
    return True, ""


def increment_usage(user_id: str, count: int = 1):
    """使用本数を加算する"""
    from core.db import increment_clips_used
    increment_clips_used(user_id, count)
