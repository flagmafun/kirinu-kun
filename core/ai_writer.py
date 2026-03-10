"""
AI ライター – Claude API を使ってYouTube Shortsのタイトル・説明文を生成
- モデル: claude-haiku-4-5（コスト最小・高速）
- フォールバック: API 未設定 or エラー時はルールベース（analyzer.py）を使用
"""
import json
import streamlit as st


def _get_api_key() -> str | None:
    """Streamlit Secrets から Anthropic API キーを取得"""
    try:
        return st.secrets["app"]["anthropic_api_key"]
    except Exception:
        return None


def _call_claude(prompt: str, api_key: str, max_tokens: int = 400) -> str | None:
    """Claude API を呼び出してテキストを返す。失敗時は None"""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception:
        return None


def generate_clip_metadata(
    clip_text: str,
    video_title: str,
    clip_index: int,
    total_clips: int,
    clip_start: float,
    clip_end: float,
) -> dict | None:
    """
    Claude API でクリップのタイトル・キャッチコピー・説明文・ハッシュタグを生成。
    API 未設定またはエラーの場合は None を返す（ルールベースへのフォールバック）。
    """
    api_key = _get_api_key()
    if not api_key or not clip_text.strip():
        return None

    position_pct = int((clip_start / max(clip_end, 1)) * 100)
    minutes = int(clip_start // 60)
    seconds = int(clip_start % 60)

    prompt = f"""あなたは日本語YouTube Shortsのバイラルタイトル専門家です。
以下の動画クリップの内容から、最高のCTRが期待できるメタデータを生成してください。

【元動画タイトル】
{video_title}

【クリップ内容（字幕/概要）】
{clip_text[:600]}

【クリップ情報】
- 位置: {minutes}:{seconds:02d} 付近（全体の{position_pct}%地点）
- クリップ番号: {clip_index}/{total_clips}

以下のJSON形式で出力してください（他の文章は不要）:
{{
  "title": "40文字以内の超バイラルタイトル（ブラケット+フック+絵文字の構成）",
  "catchphrase": "20文字以内のキャッチコピー（画面上部に表示）",
  "description": "3行以内の説明文（フック文+内容紹介+CTA）",
  "hashtags": "#Shorts #ショート動画 + 関連タグ計6個"
}}

タイトル生成のルール:
- 【】でカテゴリを囲む（例: 【衝撃】【必見】【保存版】【やばい】）
- 好奇心ギャップを作る（「実は〜」「知らないと損」「〜の正体」）
- 数字があれば積極活用（「3つの方法」「10倍速く」）
- 疑問形・感嘆形を使う
- 具体的なベネフィットを示す
- 40文字厳守・末尾に絵文字1個"""

    response = _call_claude(prompt, api_key, max_tokens=500)
    if not response:
        return None

    # JSON 部分を抽出
    try:
        # ```json ... ``` で囲まれている場合
        if "```" in response:
            response = response.split("```")[1]
            if response.startswith("json"):
                response = response[4:]
        data = json.loads(response.strip())
        # 必須キーが揃っているか確認
        for key in ("title", "catchphrase", "description", "hashtags"):
            if key not in data:
                return None
        return data
    except Exception:
        return None
