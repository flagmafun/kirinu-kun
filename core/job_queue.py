"""
同時実行スロット制御
- Supabase の processing_jobs テーブルをセマフォとして使う
- MAX_CONCURRENT 件まで同時実行を許可し、超えたらスロット確保に失敗
- 30分以上 running のレコードは stale として自動解放
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

MAX_CONCURRENT = 3    # 同時実行最大数
STALE_MINUTES  = 30  # これより古い running は stale と見なす


def _sb():
    from core.auth import get_supabase_admin
    return get_supabase_admin()


def _cleanup_stale():
    """30分以上 running のジョブを failed に更新する"""
    cutoff = (datetime.utcnow() - timedelta(minutes=STALE_MINUTES)).isoformat()
    _sb().table("processing_jobs").update({
        "status":     "failed",
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("status", "running").lt("started_at", cutoff).execute()


def get_running_count() -> int:
    """現在実行中のジョブ数を返す"""
    _cleanup_stale()
    r = _sb().table("processing_jobs") \
             .select("id", count="exact") \
             .eq("status", "running") \
             .execute()
    return r.count or 0


def acquire_slot(user_id: str) -> str | None:
    """
    実行スロットを確保する。
    成功 → job_id (str) を返す
    満杯 → None を返す
    """
    if get_running_count() >= MAX_CONCURRENT:
        return None
    job_id = str(uuid.uuid4())
    _sb().table("processing_jobs").insert({
        "id":      job_id,
        "user_id": user_id,
        "status":  "running",
    }).execute()
    return job_id


def release_slot(job_id: str, success: bool = True):
    """スロットを解放する"""
    _sb().table("processing_jobs").update({
        "status":     "done" if success else "failed",
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("id", job_id).execute()


def get_queue_depth() -> int:
    """スロット待ちの目安人数（現在実行中 - MAX_CONCURRENT、最小0）を返す"""
    return max(0, get_running_count() - MAX_CONCURRENT)
