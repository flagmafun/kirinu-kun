-- ================================================================
-- 切り抜きくん Supabase スキーマ
-- Supabase の「SQL Editor」に貼り付けて実行してください
-- ================================================================

-- ① YouTube トークン（ユーザーごと）
CREATE TABLE IF NOT EXISTS youtube_tokens (
  user_id    UUID REFERENCES auth.users(id) ON DELETE CASCADE PRIMARY KEY,
  token_json TEXT NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- ② サブスクリプション / 使用量
CREATE TABLE IF NOT EXISTS subscriptions (
  user_id                  UUID REFERENCES auth.users(id) ON DELETE CASCADE PRIMARY KEY,
  plan                     TEXT    DEFAULT 'free',
  clips_limit              INTEGER DEFAULT 10,
  clips_used_this_month    INTEGER DEFAULT 0,
  period_reset_date        DATE    DEFAULT (date_trunc('month', now()) + interval '1 month')::date,
  stripe_customer_id       TEXT,
  stripe_subscription_id   TEXT,
  status                   TEXT    DEFAULT 'active',
  updated_at               TIMESTAMPTZ DEFAULT now()
);

-- ③ RLS（Row Level Security）を有効化
ALTER TABLE youtube_tokens  ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions   ENABLE ROW LEVEL SECURITY;

-- ④ RLS ポリシー: 自分のデータのみ操作可
CREATE POLICY "Own youtube tokens"  ON youtube_tokens  FOR ALL USING (auth.uid() = user_id);
CREATE POLICY "Own subscriptions"   ON subscriptions   FOR ALL USING (auth.uid() = user_id);

-- ⑤ 新規ユーザー登録時に subscriptions レコードを自動作成
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  INSERT INTO public.subscriptions (user_id)
  VALUES (NEW.id)
  ON CONFLICT (user_id) DO NOTHING;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- ⑥ subscriptions 追加カラム（既存DBへの適用）
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS subscription_status    TEXT DEFAULT 'active';
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS current_period_end     TIMESTAMPTZ;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS youtube_approved        BOOLEAN DEFAULT false;
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS youtube_request_email  TEXT;

-- ⑦ 新規ユーザーのデフォルトを trial (5本) に変更
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  INSERT INTO public.subscriptions (user_id, plan, clips_limit)
  VALUES (NEW.id, 'trial', 5)
  ON CONFLICT (user_id) DO NOTHING;
  RETURN NEW;
END;
$$;

-- ⑧ 同時実行制御テーブル
CREATE TABLE IF NOT EXISTS processing_jobs (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  status      TEXT DEFAULT 'running',  -- 'running', 'done', 'failed'
  started_at  TIMESTAMPTZ DEFAULT now(),
  updated_at  TIMESTAMPTZ DEFAULT now()
);
ALTER TABLE processing_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Own processing jobs" ON processing_jobs FOR ALL USING (auth.uid() = user_id);

-- ⑨ 月次リセット用関数（毎月1日に Cloud Scheduler 等から呼ぶ）
CREATE OR REPLACE FUNCTION public.reset_monthly_clips()
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  UPDATE public.subscriptions
  SET clips_used_this_month = 0,
      period_reset_date = (date_trunc('month', now()) + interval '1 month')::date
  WHERE period_reset_date <= now()::date;
END;
$$;
