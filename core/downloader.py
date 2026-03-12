"""YouTube動画ダウンローダー (yt-dlp)

クライアント選択戦略:

  [Cookie あり]  ios + cookies
    - iOS API クライアント: JavaScript 不要 → Node.js 不要
    - n-challenge が存在しないため CDN URL を直接取得できる
    - cookies による認証済みセッション → SABR 実験を回避
      （SABR は非認証セッションに強制適用される: yt-dlp/yt-dlp#12482）
    ★ web + player_skip=js は NG:
      player_skip=js はフォーマット抽出まで無効化するため
      "Only images are available" になる

  [Cookie なし]  android_vr（最終手段）
    - n-challenge 不要・cookies 不要
    - 非認証のため SABR が適用される場合があり 403 になることも
"""
import subprocess
import json
import re
from pathlib import Path


def _clean_url(url: str) -> str:
    """URLから markdown 記法などの余分な文字を取り除く"""
    url = url.strip()
    url = re.sub(r'^[_*`"\']+', '', url)
    m = re.match(r'(https?://[^\s\'"<>`]+)', url)
    if not m:
        return url
    candidate = m.group(1)
    if candidate.endswith('__'):
        candidate = candidate[:-2]
    return candidate


_CREDS_DIR = Path(__file__).parent.parent / "credentials"
_COOKIES_PATH = _CREDS_DIR / "cookies.txt"


def _ensure_netscape_cookies() -> None:
    """cookies.txtがJSON形式で保存されていたらNetscape形式に変換する。
    yt-dlpはNetscape形式のみ受け付けるため、呼び出し前に必ず変換しておく。"""
    if not _COOKIES_PATH.exists() or _COOKIES_PATH.stat().st_size == 0:
        return
    content = _COOKIES_PATH.read_text(encoding="utf-8").strip()
    if not content.startswith("["):
        return  # すでにNetscape形式
    try:
        cookies_list = json.loads(content)
        lines = ["# Netscape HTTP Cookie File"]
        for c in cookies_list:
            domain = c.get("domain", "")
            flag = "TRUE" if domain.startswith(".") else "FALSE"
            path = c.get("path", "/")
            secure = "TRUE" if c.get("secure", False) else "FALSE"
            expiry = str(int(c.get("expirationDate", 0)))
            name = c.get("name", "")
            value = c.get("value", "")
            lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}")
        _COOKIES_PATH.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass


# cookies が失効/セッション切れを示すキーワード
_COOKIES_EXPIRED_HINTS = (
    "no longer valid",
    "cookies are no longer valid",
    "have likely been rotated",
    "the page needs to be reloaded",
    "page needs to be reloaded",
)

_COOKIES_UPDATE_MSG = (
    "cookies が期限切れまたはセッションが失効しています。\n\n"
    "📋 **解決方法: cookies を再エクスポートしてください**\n"
    "1. Chrome で YouTube にログインした状態で\n"
    "2. 「Get cookies.txt LOCALLY」拡張 → Export → youtube.com のみ保存\n"
    "3. 管理パネルの「🍪 YouTube Cookies 管理」→ 貼り付けて保存\n"
)


def _cookies_expired_in_stderr(stderr: str) -> bool:
    s = stderr.lower()
    return any(h in s for h in _COOKIES_EXPIRED_HINTS)


def _get_ytdlp_base(use_cookies: bool = True) -> list:
    """yt-dlp共通オプションを返す。

    Cookieあり:   ios クライアント（JavaScript不要、認証済みセッション）
    Cookieなし:   android_vr（n-challenge 不要、ただし SABR の影響あり）
    """
    _ensure_netscape_cookies()
    has_cookies = use_cookies and _COOKIES_PATH.exists() and _COOKIES_PATH.stat().st_size > 0
    opts = ["--no-playlist", "--no-check-certificates"]

    if has_cookies:
        # ios クライアント:
        #   - JavaScript 不要（n-challenge なし）→ Node.js 不要
        #   - cookies による認証で SABR を回避
        #   - 完全なフォーマット一覧を返す
        opts += [
            "--extractor-args", "youtube:player_client=ios",
            "--cookies", str(_COOKIES_PATH),
        ]
    else:
        # android_vr: n-challenge 不要だが非認証のため SABR が発生する場合あり
        opts += ["--extractor-args", "youtube:player_client=android_vr"]

    return opts


def get_video_info(url: str) -> dict:
    """動画のメタ情報を取得"""
    url = _clean_url(url)
    result = subprocess.run(
        ["yt-dlp", "--dump-json"] + _get_ytdlp_base() + [url],
        capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def download_video(url: str, output_dir: Path, progress_callback=None) -> Path:
    """YouTube動画をmp4でダウンロードして返す"""
    url = _clean_url(url)
    output_dir.mkdir(parents=True, exist_ok=True)

    base = _get_ytdlp_base()
    has_cookies = _COOKIES_PATH.exists() and _COOKIES_PATH.stat().st_size > 0

    # video_id 取得
    id_result = subprocess.run(
        ["yt-dlp", "--print", "id"] + base + [url],
        capture_output=True, text=True
    )
    if id_result.returncode != 0:
        _stderr = (id_result.stderr or id_result.stdout or "").strip()
        if has_cookies and _cookies_expired_in_stderr(_stderr):
            # cookies 期限切れ/セッション失効
            # ★ android_vr フォールバックはしない（SABR で必ず失敗するため）
            raise RuntimeError(
                _COOKIES_UPDATE_MSG + f"\n詳細: {_stderr[-400:]}"
            )
        raise RuntimeError(
            f"yt-dlp --print id 失敗 (code {id_result.returncode}):\n{_stderr[-600:]}"
        )
    video_id = id_result.stdout.strip()
    output_template = str(output_dir / f"{video_id}.%(ext)s")

    # ios クライアントで取得可能なフォーマット（プログレッシブ MP4 優先）
    # 22: 720p mp4 (video+audio), 18: 360p mp4 (video+audio)
    fmt = "22/18/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

    cmd = ["yt-dlp", "-f", fmt, "--merge-output-format", "mp4",
           "-o", output_template] + base + [url]

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")
        # SABR 検出
        if "sabr" in err.lower() or "missing a url" in err.lower():
            raise RuntimeError(
                "YouTube SABR エラー（非認証セッション）\n\n"
                "YouTube が SABR-only streaming を適用しています。\n"
                "有効な cookies を設定すると回避できます。\n\n"
                + _COOKIES_UPDATE_MSG
                + f"\n詳細: {err[-300:]}"
            )
        if "HTTP Error 403" in err or "403: Forbidden" in err:
            if has_cookies:
                raise RuntimeError(
                    "YouTube CDN 403エラー\n\n"
                    "cookies が期限切れか無効の可能性があります。\n"
                    + _COOKIES_UPDATE_MSG
                    + f"\n詳細: {err[-300:]}"
                )
            raise RuntimeError(
                "YouTube CDN 403エラー（IP制限）\n\n"
                "Streamlit Cloud のIPがブロックされています。\n"
                "cookies を設定することで回避できる場合があります。\n\n"
                f"詳細: {err[-300:]}"
            )
        raise RuntimeError(f"yt-dlp失敗 (code {result.returncode}): {err[-500:]}")

    for ext in [".mp4", ".mkv", ".webm", ".m4v", ".mov"]:
        path = output_dir / f"{video_id}{ext}"
        if path.exists():
            return path

    candidates = [
        p for p in output_dir.glob(f"{video_id}.*")
        if p.suffix not in {".part", ".ytdl", ".json"}
    ]
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_size)

    existing = [p.name for p in output_dir.iterdir()]
    raise FileNotFoundError(
        f"ダウンロードファイルが見つかりません: {video_id}\n"
        f"output_dir内のファイル: {existing}"
    )


def check_cookies_validity(
    test_url: str = "https://www.youtube.com/watch?v=jNQXAC9IVRw",
) -> tuple:
    """
    cookies の有効性を確認（ios クライアントで --print id テスト）。
    Returns: (is_valid: bool, message: str)
    """
    _ensure_netscape_cookies()
    has_cookies = _COOKIES_PATH.exists() and _COOKIES_PATH.stat().st_size > 0
    if not has_cookies:
        return False, "cookies が設定されていません"

    base = _get_ytdlp_base(use_cookies=True)
    try:
        result = subprocess.run(
            ["yt-dlp", "--print", "id"] + base + [test_url],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "タイムアウト（30秒）"

    if result.returncode == 0 and result.stdout.strip():
        return True, "✅ cookies は有効です"

    stderr = (result.stderr or result.stdout or "").strip()
    if _cookies_expired_in_stderr(stderr):
        return False, "❌ cookies が期限切れです（再エクスポートが必要）"
    return False, f"❌ 確認失敗: {stderr[-200:]}"
