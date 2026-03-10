"""
動画解析モジュール
- 動画情報取得
- 字幕/トランスクリプト取得
- 10クリップ自動選定
"""
import subprocess
import json
import re
from pathlib import Path
from core.downloader import _get_ytdlp_base, _clean_url


# ──────────────────────────────────────────────────────────
# 動画情報
# ──────────────────────────────────────────────────────────

def get_video_info(url: str) -> dict:
    """yt-dlp で動画メタ情報を取得"""
    url = _clean_url(url)
    result = subprocess.run(
        ["yt-dlp", "--dump-json"] + _get_ytdlp_base() + [url],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        err_tail = result.stderr.strip()[-600:] if result.stderr else "(no stderr)"
        raise RuntimeError(f"yt-dlp失敗 (code {result.returncode}):\n{err_tail}")
    if not result.stdout.strip():
        raise RuntimeError("yt-dlp が空のレスポンスを返しました")
    info = json.loads(result.stdout)
    return {
        "url":        url,
        "id":         info.get("id", ""),
        "title":      info.get("title", ""),
        "duration":   float(info.get("duration") or 0),
        "thumbnail":  info.get("thumbnail", ""),
        "uploader":   info.get("uploader", ""),
        "view_count": info.get("view_count", 0),
        "chapters":   info.get("chapters") or [],
        "description": info.get("description", ""),
    }


# ──────────────────────────────────────────────────────────
# 字幕取得
# ──────────────────────────────────────────────────────────

def get_transcript(url: str, work_dir: Path) -> list:
    """
    字幕（日本語 → 英語 → 自動生成）を取得してパース。
    youtube-transcript-api を使用（CDN不要・IP制限回避）。
    失敗時は空リストを返す。
    """
    # video_id を URL から抽出
    video_id = None
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", url)
    if m:
        video_id = m.group(1)
    if not video_id:
        return []

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        # 日本語 → 英語 の優先順でトランスクリプトを取得（手動・自動生成どちらも対象）
        segs = YouTubeTranscriptApi.get_transcript(video_id, languages=["ja", "en"])
        if segs:
            return [
                {
                    "start": s["start"],
                    "end":   s["start"] + s.get("duration", 3.0),
                    "text":  s["text"].replace("\n", " ").strip(),
                }
                for s in segs
                if s.get("text", "").strip()
            ]
    except Exception:
        pass  # フォールバック: yt-dlp で取得

    # フォールバック: yt-dlp（CDN経由・IP制限を受ける可能性あり）
    work_dir.mkdir(parents=True, exist_ok=True)
    for lang, auto in [("ja", False), ("ja", True), ("en", False), ("en", True)]:
        flag = "--write-auto-subs" if auto else "--write-subs"
        cmd = [
            "yt-dlp", "--skip-download",
            flag, "--sub-langs", lang, "--sub-format", "json3",
            "-o", str(work_dir / "%(id)s"),
        ] + _get_ytdlp_base() + [url]
        subprocess.run(cmd, capture_output=True, text=True)
        for f in work_dir.glob(f"*.{lang}.json3"):
            subs = _parse_json3(f)
            if subs:
                return subs

    return []


def _parse_json3(path: Path) -> list:
    """YouTube json3 字幕をパース → [{start, end, text}, ...]"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out = []
        for ev in data.get("events", []):
            segs = ev.get("segs")
            if not segs:
                continue
            text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
            if not text:
                continue
            start = ev["tStartMs"] / 1000
            dur   = ev.get("dDurationMs", 3000) / 1000
            out.append({"start": start, "end": start + dur, "text": text})
        return out
    except Exception:
        return []


# ──────────────────────────────────────────────────────────
# クリップ自動選定
# ──────────────────────────────────────────────────────────

def auto_select_clips(
    duration:    float,
    transcript:  list,
    n_clips:     int = 10,
    clip_sec:    int = 60,
    video_title: str = "",
) -> list:
    """
    動画を n_clips ゾーンに分割し、各ゾーンから最適な
    開始点を選んで clip_sec 秒のクリップを生成する。
    """
    if duration <= 0 or n_clips <= 0:
        return []

    zone = duration / n_clips
    clips = []

    for i in range(n_clips):
        z_start = i * zone
        z_end   = z_start + zone

        zone_subs = [t for t in transcript if z_start <= t["start"] < z_end]

        if zone_subs:
            clip_start = max(0.0, zone_subs[0]["start"])
        else:
            clip_start = z_start

        clip_start = round(clip_start, 1)
        clip_end   = round(min(clip_start + clip_sec, duration), 1)

        clip_subs = [t for t in transcript if clip_start <= t["start"] < clip_end]
        clip_text = " ".join(t["text"] for t in clip_subs)

        scores = _score_clip(clip_text, clip_subs, clip_end - clip_start)

        clips.append({
            "index":              i + 1,
            "start":              clip_start,
            "end":                clip_end,
            "transcript":         clip_text[:400],
            "title":              _suggest_title(clip_text, video_title),
            "catchphrase":        _suggest_catchphrase(clip_text),
            "description":        _generate_description(clip_text, video_title),
            "hashtags":           _suggest_hashtags(clip_text, video_title),
            "enabled":            True,
            "score":              scores["score"],
            "score_density":      scores["score_density"],
            "score_engagement":   scores["score_engagement"],
            "score_completeness": scores["score_completeness"],
        })

    return clips


# ──────────────────────────────────────────────────────────
# タイトル生成
# ──────────────────────────────────────────────────────────

# 【ブラケット】ルール（最初にマッチしたものを使用）
_BRACKET_RULES = [
    (["誰も教えてくれない", "知らなかった", "実は知らない"],  "【知らなかった】"),
    (["衝撃", "信じられない", "ショック", "まさか"],         "【衝撃】"),
    (["秘密", "裏側", "内緒", "極秘", "非公開"],            "【極秘】"),
    (["ヤバい", "やばい", "ヤバすぎ"],                      "【ヤバすぎ】"),
    (["危険", "注意", "リスク", "気をつけ", "落とし穴"],     "【要注意】"),
    (["初心者", "入門", "はじめて", "ゼロから"],             "【初心者必見】"),
    (["プロ", "専門家", "本物", "一流"],                    "【プロが教える】"),
    (["コツ", "秘訣", "裏技", "テクニック"],                "【裏技】"),
    (["保存", "まとめ", "総まとめ", "完全版"],              "【保存版】"),
    (["最強", "最高", "神", "完璧"],                       "【最強】"),
    (["お金", "稼", "副業", "投資"],                       "【お金の話】"),
    (["限定", "今だけ", "特別"],                           "【限定公開】"),
]

# 絵文字ルール（コンテンツカテゴリーに対応）
_EMOJI_RULES = [
    (["お金", "投資", "稼", "副業", "収入", "FIRE"],  "💰"),
    (["AI", "テクノロジー", "技術", "プログラミング"], "🤖"),
    (["危険", "リスク", "注意", "要注意", "落とし穴"], "⚠️"),
    (["衝撃", "ヤバ", "やばい", "信じられない"],       "😱"),
    (["コツ", "秘訣", "方法", "ポイント", "裏技"],     "💡"),
    (["最強", "最高", "最上", "神"],                   "🏆"),
    (["なぜ", "理由", "どうして", "謎", "不思議"],     "🤔"),
    (["簡単", "すぐ", "即", "一瞬"],                   "✨"),
    (["プロ", "専門", "本物", "一流"],                 "👑"),
    (["健康", "体", "ダイエット", "筋トレ"],           "💪"),
    (["勉強", "学習", "知識", "スキル"],               "📚"),
    (["ビジネス", "起業", "経営", "スタートアップ"],   "🚀"),
    (["美容", "スキンケア", "メイク"],                 "✨"),
    (["旅行", "観光", "海外"],                         "✈️"),
    (["料理", "レシピ", "グルメ"],                     "🍳"),
]

_HOOK_WEIGHTS = {
    "誰も教えてくれない": 12, "知らないと損": 11, "見逃し厳禁": 10,
    "実は": 9, "秘密": 9, "衝撃": 9,
    "ヤバい": 8, "やばい": 8, "ヤバすぎ": 8,
    "驚き": 7, "衝撃的": 7, "秘訣": 8,
    "なぜ": 7, "どうして": 6, "理由": 6,
    "危険": 7, "注意": 6, "リスク": 6, "落とし穴": 8,
    "絶対": 6, "必ず": 6, "確実": 6,
    "コツ": 6, "裏技": 7, "ポイント": 5,
    "最強": 6, "最高": 5, "神": 6,
    "即効": 7, "一瞬で": 6, "すぐ": 5,
    "プロが": 8, "専門家が": 8,
    "すごい": 5, "凄い": 6,
    "限定": 7, "今だけ": 7,
    "必見": 7, "保存版": 7,
}


def _suggest_title(text: str, video_title: str = "", max_len: int = 50) -> str:
    """
    YouTube Shorts の CTR を最大化するタイトルを生成。
    構成: 【ブラケット】＋ フック文 ＋ 絵文字
    - 日本語 Shorts で最も CTR が高い「ブラケット+絵文字」フォーマット
    - フックワードをスコアリングして最も引きのある文を選択
    - 疑問形・数字表現を優先
    """
    combined = text + " " + video_title
    if not text:
        return ""

    # 文に分割（6〜48文字）
    sentences = re.split(r"[。！？!?]+", text)
    sentences = [s.strip() for s in sentences if 6 <= len(s.strip()) <= 48]
    if not sentences:
        return text[:max_len].rstrip("、，, 　")

    best_score = -1
    best_sent  = sentences[0]

    for sent in sentences:
        score = 0
        for word, pts in _HOOK_WEIGHTS.items():
            if word in sent:
                score += pts
        # 疑問形は高 CTR
        if re.search(r"[？?]|なのか|でしょう|ますか|のか$", sent):
            score += 7
        # 数量表現「3つの方法」「10分で」
        if re.search(r"[0-9０-９一二三四五六七八九十百千]+[つ個本冊点分秒倍位割]", sent):
            score += 6
        # 最適文字数 15〜40 にボーナス
        if 15 <= len(sent) <= 40:
            score += 4
        elif len(sent) < 8:
            score -= 4
        if score > best_score:
            best_score = score
            best_sent  = sent

    title = best_sent.strip().rstrip("、，,　 。")

    # 末尾句読点
    if not re.search(r"[！？!?]$", title):
        if re.search(r"^(なぜ|どうして|どうやって|何|いつ|誰|どこ)", title):
            title += "？"
        else:
            title += "！"

    # ブラケット選択
    bracket = ""
    for keywords, br in _BRACKET_RULES:
        if any(kw in combined for kw in keywords):
            bracket = br
            break
    if not bracket:
        bracket = "【必見】"  # デフォルト

    # 絵文字選択
    emoji_suffix = "🔥"
    for keywords, em in _EMOJI_RULES:
        if any(kw in combined for kw in keywords):
            emoji_suffix = em
            break

    # 組み立て
    full = f"{bracket}{title}{emoji_suffix}"
    if len(full) > max_len:
        # ブラケットなし
        full = f"{title}{emoji_suffix}"
    if len(full) > max_len:
        full = title[: max_len - 2] + "…" + emoji_suffix

    return full[:max_len]


# ──────────────────────────────────────────────────────────
# キャッチコピー生成
# ──────────────────────────────────────────────────────────

_CATCHPHRASE_RULES = [
    (["誰も教えてくれない", "知らなかった", "知らない人"],  "知らないと損！👀"),
    (["秘密", "裏側", "内緒", "極秘"],                     "秘密を大公開⚡"),
    (["衝撃", "ショック", "信じられない", "まさか"],        "衝撃の事実😱"),
    (["ヤバい", "やばい", "ヤバすぎ"],                     "これはヤバい🔥"),
    (["すごい", "スゴい", "凄い", "神"],                   "思わず驚く😲"),
    (["なぜ", "どうして", "理由", "謎"],                   "その理由とは？🤔"),
    (["危険", "注意", "リスク", "落とし穴"],               "要注意！⚠️"),
    (["コツ", "秘訣", "裏技", "テクニック"],               "知って得するコツ💡"),
    (["お金", "稼", "副業", "投資", "収入"],               "お金の話💰"),
    (["プロ", "専門家", "一流"],                           "プロが語る👑"),
    (["AI", "テクノロジー", "最新"],                       "最新情報🤖"),
    (["最強", "最高", "最上"],                             "これが最強🏆"),
    (["簡単", "すぐ", "即", "一瞬"],                      "すぐ使える✨"),
    (["初心者", "入門", "はじめて"],                       "初心者必見📖"),
    (["絶対", "必ず", "確実"],                             "絶対に見て！🎯"),
    (["方法", "やり方", "やってみた"],                     "試してみた✅"),
    (["健康", "体", "ダイエット"],                         "健康の秘訣💪"),
    (["ビジネス", "起業", "経営"],                         "ビジネスの本音🚀"),
    (["面白", "笑", "ネタ", "爆笑"],                       "思わず笑う😂"),
    (["重要", "大事", "大切"],                             "超重要ポイント⚠️"),
]

_CATCHPHRASE_DEFAULTS = [
    "保存必須📌",
    "見逃し厳禁🔔",
    "要チェック✅",
    "今すぐ見て👇",
    "これは必見🎯",
    "知ってた？👀",
]


def _suggest_catchphrase(clip_text: str) -> str:
    """
    クリップ内容に合ったキャッチコピーを生成（〜25文字）。
    タイトルバー上部に表示されるフレーズ。FOMO・感情喚起を優先。
    """
    for keywords, phrase in _CATCHPHRASE_RULES:
        if any(kw in clip_text for kw in keywords):
            return phrase

    idx = sum(ord(c) for c in clip_text[:20]) % len(_CATCHPHRASE_DEFAULTS)
    return _CATCHPHRASE_DEFAULTS[idx]


# ──────────────────────────────────────────────────────────
# 説明文生成
# ──────────────────────────────────────────────────────────

# カテゴリー別フック文（動画内容の雰囲気に合わせて自動選択）
_DESC_HOOKS = [
    (["お金", "投資", "副業", "稼", "資産", "FIRE", "節約", "収入"],
     "お金の知識は、知っているだけで人生が変わります。"),
    (["AI", "ChatGPT", "GPT", "人工知能", "プログラミング", "エンジニア", "テクノロジー"],
     "AIとテクノロジーの最前線を、わかりやすくお届けします。"),
    (["ビジネス", "起業", "経営", "マーケティング", "SNS", "集客", "ブランド"],
     "成功するビジネスには、必ず理由があります。"),
    (["勉強", "学習", "受験", "スキル", "資格", "成長", "読書"],
     "正しい学び方を知るだけで、成長スピードが変わります。"),
    (["健康", "ダイエット", "筋トレ", "食事", "運動", "体"],
     "体の変化は、小さな習慣の積み重ねから始まります。"),
    (["転職", "就活", "キャリア", "仕事", "サラリーマン", "会社"],
     "キャリアは、正しい情報と選択で変えられます。"),
    (["心理", "人間関係", "コミュニケーション", "メンタル", "思考"],
     "人間関係を円滑にするヒントが詰まっています。"),
    (["料理", "レシピ", "グルメ", "食べ", "クッキング"],
     "思わず試したくなるレシピをご紹介します。"),
    (["旅行", "観光", "海外", "トラベル", "ホテル"],
     "旅がもっと楽しくなる情報をお届けします。"),
    (["面白", "笑", "ネタ", "爆笑", "バズ", "エンタメ"],
     "思わず最後まで見てしまう動画です。"),
]


def _generate_description(clip_text: str, video_title: str = "") -> str:
    """
    YouTube Shorts の登録者・再生数を最大化する説明文を生成。

    構成:
      1. カテゴリー別フック文（検索結果フィードで最初に見える最重要箇所）
      2. コンテンツ紹介（エンゲージメントの高い文を厳選して箇条書き）
      3. CTA （チャンネル登録・関連動画への誘導）
    """
    combined = clip_text + " " + video_title

    # 1. フック文（カテゴリー自動判定）
    hook = "見ると得する情報をギュッと凝縮してお届けします。"
    for keywords, template in _DESC_HOOKS:
        if any(kw in combined for kw in keywords):
            hook = template
            break

    # 2. コンテンツ紹介（スコアの高い文を最大2文ピック）
    sentences = re.split(r"[。！？.!?]+", clip_text) if clip_text else []
    sentences = [s.strip() for s in sentences if 8 <= len(s.strip()) <= 60]

    _INTEREST_MARKERS = [
        "？", "！", "すごい", "ヤバ", "なぜ", "秘密", "実は",
        "重要", "ポイント", "注目", "驚", "絶対", "必見", "意外",
    ]
    scored = sorted(
        [(sum(sent.count(m) for m in _INTEREST_MARKERS), sent)
         for sent in sentences],
        reverse=True,
    )

    teaser_parts = [f"・{sent}" for _, sent in scored[:2]]

    parts = [hook]
    if teaser_parts:
        parts.append("\n".join(teaser_parts))
    parts.append(_build_cta())

    return "\n\n".join(parts)[:500]


def _build_cta() -> str:
    """
    登録者を増やすための CTA テンプレート。
    - チャンネル登録 ＋ 通知 ON が最重要
    - コメント促進で滞在時間・エンゲージメント向上
    """
    return (
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔔 チャンネル登録＋通知ON で最新動画を見逃さない！\n"
        "💬 感想・質問はコメントで教えてください👇\n"
        "❤️ 参考になったらいいね！をお願いします"
    )


# ──────────────────────────────────────────────────────────
# ハッシュタグ生成
# ──────────────────────────────────────────────────────────

def _suggest_hashtags(clip_text: str, video_title: str = "") -> str:
    """
    YouTube Shorts の再生数・発見率を最大化するハッシュタグ生成。

    戦略（YouTube アルゴリズム対策）:
      #Shorts（必須）+ カテゴリー高ボリューム2個 + ニッチ1〜2個 = 計4〜6個
      ※ 多すぎるとスパム判定 → 上限6個厳守
    """
    combined = clip_text + " " + video_title

    # ① 必須タグ（Shorts フィードへの露出に必要）
    tags = ["#Shorts", "#ショート動画"]

    # ② カテゴリータグ（高ボリューム × 関連性で最大2個）
    _CATEGORY_RULES = [
        (["AI", "ChatGPT", "GPT", "人工知能", "機械学習", "LLM", "Gemini"],
         ["#AI活用", "#ChatGPT"]),
        (["ビジネス", "起業", "経営", "スタートアップ", "会社"],
         ["#ビジネス", "#起業家"]),
        (["お金", "投資", "株", "資産", "FIRE", "節約"],
         ["#お金", "#資産形成"]),
        (["副業", "稼", "収入", "フリーランス"],
         ["#副業", "#収入アップ"]),
        (["プログラミング", "コード", "エンジニア", "開発", "Python", "IT"],
         ["#プログラミング", "#エンジニア"]),
        (["勉強", "学習", "受験", "資格", "スキルアップ"],
         ["#勉強法", "#スキルアップ"]),
        (["マーケティング", "SNS", "集客", "ブランディング", "YouTube"],
         ["#マーケティング", "#SNS運用"]),
        (["転職", "就活", "キャリア", "面接", "仕事", "サラリーマン"],
         ["#転職", "#キャリアアップ"]),
        (["健康", "ダイエット", "筋トレ", "運動", "食事"],
         ["#健康", "#ダイエット"]),
        (["料理", "レシピ", "食べ", "グルメ", "クッキング"],
         ["#料理", "#レシピ"]),
        (["旅行", "観光", "海外", "ホテル", "トラベル"],
         ["#旅行", "#国内旅行"]),
        (["ゲーム", "ゲーミング", "攻略", "ゲーマー"],
         ["#ゲーム", "#Gaming"]),
        (["音楽", "歌", "ライブ", "アーティスト"],
         ["#音楽", "#ライブ"]),
        (["美容", "スキンケア", "メイク", "化粧", "コスメ"],
         ["#美容", "#スキンケア"]),
        (["ファッション", "コーデ", "おしゃれ", "ブランド"],
         ["#ファッション", "#コーデ"]),
        (["インタビュー", "対談", "対話", "ゲスト"],
         ["#インタビュー", "#対談"]),
        (["面白", "笑", "ネタ", "爆笑", "バズ"],
         ["#面白い", "#バズり動画"]),
        (["心理", "脳", "メンタル", "思考"],
         ["#心理学", "#メンタル"]),
        (["不動産", "住宅", "マンション", "家"],
         ["#不動産", "#住宅"]),
        (["英語", "語学", "留学", "TOEIC"],
         ["#英語学習", "#語学"]),
    ]

    for keywords, cat_tags in _CATEGORY_RULES:
        if any(kw in combined for kw in keywords):
            tags += cat_tags
            break

    # ③ ニッチタグ（視聴者属性・内容属性でさらに絞り込み → 濃いファン獲得）
    _NICHE_RULES = [
        (["初心者", "入門", "はじめて", "ゼロから"],     "#初心者向け"),
        (["解説", "わかりやすい", "まとめ", "図解"],      "#わかりやすい解説"),
        (["体験", "実体験", "経験談", "実話"],            "#実体験"),
        (["裏技", "テクニック", "コツ", "秘訣"],          "#裏技"),
        (["失敗", "後悔", "反省", "教訓"],               "#失敗談"),
        (["海外", "外国", "グローバル", "英語"],          "#海外"),
        (["サラリーマン", "会社員", "副業"],              "#サラリーマン"),
        (["主婦", "育児", "子育て", "ママ"],             "#子育て"),
        (["20代", "30代", "40代", "若者"],               "#20代"),
    ]

    for keywords, niche_tag in _NICHE_RULES:
        if any(kw in combined for kw in keywords):
            if niche_tag not in tags:
                tags.append(niche_tag)
            break

    # ④ エンゲージメント促進タグ（コメント・保存を促す）
    _ENGAGEMENT_RULES = [
        (["知らなかった", "驚", "衝撃"],  "#知らなかった"),
        (["保存", "まとめ", "永久保存"],  "#保存版"),
        (["試してみた", "やってみた"],    "#やってみた"),
    ]
    for keywords, eng_tag in _ENGAGEMENT_RULES:
        if any(kw in combined for kw in keywords):
            if eng_tag not in tags:
                tags.append(eng_tag)
            break

    return " ".join(tags[:6])  # 最大6個（スパム判定防止）


# ──────────────────────────────────────────────────────────
# スコアリング
# ──────────────────────────────────────────────────────────

def _score_clip(clip_text: str, clip_subs: list, duration: float) -> dict:
    """
    クリップのスコアを算出（0〜100点）
    - 文字密度  (0-40): 情報量の多さ（文字数/秒）
    - 盛り上がり(0-40): エンゲージメント指標の数
    - 文章完成度(0-20): 字幕セグメントの密度
    """
    char_count = len(clip_text.replace(" ", "").replace("　", ""))
    chars_per_sec = char_count / max(duration, 1)
    density_score = min(int(chars_per_sec / 12 * 40), 40)

    markers = [
        "？", "！", "?", "!", "驚", "すごい", "ヤバ", "やばい",
        "重要", "ポイント", "なぜ", "どうして", "秘密", "実は",
        "必ず", "絶対", "危険", "注目", "衝撃", "緊急",
    ]
    engagement_count = sum(clip_text.count(m) for m in markers)
    engagement_score = min(engagement_count * 8, 40)

    completeness_score = min(int(len(clip_subs) / 6 * 20), 20)

    total = density_score + engagement_score + completeness_score
    return {
        "score":              min(total, 100),
        "score_density":      density_score,
        "score_engagement":   engagement_score,
        "score_completeness": completeness_score,
    }


# ──────────────────────────────────────────────────────────
# ユーティリティ
# ──────────────────────────────────────────────────────────

def fmt_time(sec: float) -> str:
    """秒 → MM:SS"""
    s = int(sec)
    return f"{s // 60:02d}:{s % 60:02d}"


def fmt_duration(sec: float) -> str:
    """秒 → HH:MM:SS or MM:SS"""
    s = int(sec)
    h = s // 3600
    m = (s % 3600) // 60
    ss = s % 60
    return f"{h}:{m:02d}:{ss:02d}" if h else f"{m:02d}:{ss:02d}"
