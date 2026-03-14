"""
Stripe Webhook エンドポイント (FastAPI)

Railway で別サービスとして起動:
  uvicorn webhook:app --host 0.0.0.0 --port 8000

必要な環境変数:
  STRIPE_SECRET_KEY      - Stripe シークレットキー
  STRIPE_WEBHOOK_SECRET  - Stripe Webhook 署名シークレット
  STRIPE_PRICE_BASIC     - ベーシックプランの Stripe Price ID
  STRIPE_PRICE_PRO       - プロプランの Stripe Price ID
  SUPABASE_URL           - Supabase プロジェクト URL
  SUPABASE_SERVICE_ROLE_KEY - Supabase サービスロールキー
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import stripe
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# /app 配下のモジュールを import できるようにする
sys.path.insert(0, str(Path(__file__).parent))

app = FastAPI()

stripe.api_key     = os.environ.get("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET     = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_BASIC = os.environ.get("STRIPE_PRICE_BASIC", "")
STRIPE_PRICE_PRO   = os.environ.get("STRIPE_PRICE_PRO",   "")

# Price ID → (plan名, clips_limit)
PLAN_BY_PRICE: dict[str, tuple[str, int]] = {
    STRIPE_PRICE_BASIC: ("basic", 100),
    STRIPE_PRICE_PRO:   ("pro",   500),
}


def _sb():
    from supabase import create_client
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(url, key)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError as e:
        raise HTTPException(status_code=400, detail=f"Invalid signature: {e}")

    sb        = _sb()
    event_type = event["type"]
    data       = event["data"]["object"]

    # ── 決済完了 ──────────────────────────────────────────────────────
    if event_type == "checkout.session.completed":
        user_id     = data.get("client_reference_id")  # Streamlit が埋め込む
        customer_id = data.get("customer")
        sub_id      = data.get("subscription")
        price_id    = data.get("metadata", {}).get("price_id", "")
        plan, limit = PLAN_BY_PRICE.get(price_id, ("basic", 100))
        period_end  = (datetime.utcnow() + timedelta(days=31)).isoformat()

        if user_id:
            sb.table("subscriptions").upsert({
                "user_id":                user_id,
                "plan":                   plan,
                "clips_limit":            limit,
                "stripe_customer_id":     customer_id,
                "stripe_subscription_id": sub_id,
                "subscription_status":    "active",
                "current_period_end":     period_end,
                "updated_at":             datetime.utcnow().isoformat(),
            }, on_conflict="user_id").execute()

    # ── サブスク解約・停止 ────────────────────────────────────────────
    elif event_type in ("customer.subscription.deleted", "customer.subscription.paused"):
        customer_id = data.get("customer")
        if customer_id:
            sb.table("subscriptions").update({
                "plan":                "trial",
                "clips_limit":         5,
                "subscription_status": "canceled",
                "updated_at":          datetime.utcnow().isoformat(),
            }).eq("stripe_customer_id", customer_id).execute()

    # ── 支払い失敗 ────────────────────────────────────────────────────
    elif event_type == "invoice.payment_failed":
        sub_id = data.get("subscription")
        if sub_id:
            sb.table("subscriptions").update({
                "subscription_status": "past_due",
                "updated_at":          datetime.utcnow().isoformat(),
            }).eq("stripe_subscription_id", sub_id).execute()

    return JSONResponse({"ok": True})


@app.get("/create-checkout")
async def create_checkout(plan: str, user_id: str, app_url: str = ""):
    """
    Stripe Checkout セッションを作成して URL を返す。
    Streamlit から呼び出す。
    """
    price_id = STRIPE_PRICE_BASIC if plan == "basic" else STRIPE_PRICE_PRO
    if not price_id:
        raise HTTPException(400, "Price ID が設定されていません")

    success_url = f"{app_url}?payment=success" if app_url else "https://example.com?payment=success"
    cancel_url  = f"{app_url}?payment=canceled" if app_url else "https://example.com?payment=canceled"

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=user_id,
        metadata={"price_id": price_id},
    )
    return {"url": session.url}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
