"""YouTube動画ダウンローダー (yt-dlp)

クライアント選択戦略:
  Cookieあり → web クライアント + --js-runtimes node
    - Node.jsがn-challengeを解決（requirements.txt の nodejs-wheel を使用）
    - yt-dlp 2026.03以降はDenoがデフォルト → 明示的に node を指定
    - 認証済みCDN URLでデータセンターIPブロックを回避
    - yt-dlp-ejs (pip) がEJSスクリプト配布を担当
    - nodejs-wheel (pip) がNode.js 22.6+ バイナリを提供（packages.txt 不要）
  Cookieなし → android_vr クライアント
    - ratebypass=yes → n-challenge不要・Deno不要
    - PO Token不要
    - ただしStreamlit CloudのデータセンターIPはCDNにブロックされる場合あり
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


_COOKIES_EXPIRED_HINTS = (
    "no longer valid",
    "cookies are no longer valid",
    "have likely been rotated",
)

def _cookies_expired_in_stderr(stderr: str) -> bool:
    s = stderr.lower()
    return any(h in s for h in _COOKIES_EXPIRED_HINTS)


def _get_ytdlp_base(use_cookies: bool = True) -> list[str]:
    """yt-dlp共通オプションを返す。use_cookies=False で android_vr 強制。"""
    _ensure_netscape_cookies()
    has_cookies = use_cookies and _COOKIES_PATH.exists() and _COOKIES_PATH.stat().st_size > 0
    opts = ["--no-playlist", "--no-check-certificates"]

    if has_cookies:
        # Cookieあり: webクライアント + Node.jsでn-challenge解決
        opts += [
            "--extractor-args", "youtube:player_client=web",
            "--cookies", str(_COOKIES_PATH),
            "--js-runtimes", "node",
        ]
    else:
        # Cookieなし / 期限切れフォールバック: android_vrクライアント
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
    _cookies_expired_fallback = False  # 期限切れで android_vr にフォールバックしたか

    # video_id取得（cookies 期限切れ時は android_vr にフォールバック）
    id_result = subprocess.run(
        ["yt-dlp", "--print", "id"] + base + [url],
        capture_output=True, text=True
    )
    if id_result.returncode != 0:
        _stderr = (id_result.stderr or id_result.stdout or "").strip()
        if has_cookies and _cookies_expired_in_stderr(_stderr):
            # cookies 期限切れ → android_vr でリトライ
            base = _get_ytdlp_base(use_cookies=False)
            has_cookies = False
            _cookies_expired_fallback = True
            id_result = subprocess.run(
                ["yt-dlp", "--print", "id"] + base + [url],
                capture_output=True, text=True
            )
            if id_result.returncode != 0:
                _stderr2 = (id_result.stderr or id_result.stdout or "").strip()
                raise RuntimeError(
                    "cookies が期限切れで、android_vr フォールバックも失敗しました。\n"
                    "YouTubeの cookies を再エクスポートして Streamlit Secrets を更新してください。\n\n"
                    f"詳細: {_stderr2[-400:]}"
                )
        else:
            raise RuntimeError(
                f"yt-dlp --print id 失敗 (code {id_result.returncode}):\n{_stderr[-600:]}"
            )
    video_id = id_result.stdout.strip()
    output_template = str(output_dir / f"{video_id}.%(ext)s")

    # フォーマット選択:
    #   Cookieあり（web + node）: 720p DASH + audio も取れる（認証済みCDN）
    #   Cookieなし / フォールバック（android_vr）: format 18のみ安全
    if has_cookies:
        fmt = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/18/best"
    else:
        fmt = "18/best"

    cmd = ["yt-dlp", "-f", fmt, "--merge-output-format", "mp4",
           "-o", output_template] + base + [url]

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")
        if "HTTP Error 403" in err or "403: Forbidden" in err:
            if _cookies_expired_fallback:
                raise RuntimeError(
                    "cookies が期限切れのため、android_vr フォールバックでもダウンロードできませんでした。\n\n"
                    "📋 **解決方法: cookies を再エクスポートしてください**\n"
                    "1. Chromeに「Get cookies.txt LOCALLY」拡張をインストール\n"
                    "2. YouTubeにログインした状態で拡張をクリック → Export → youtube.com のみ保存\n"
                    "3. Streamlit Cloud → Settings → Secrets の [youtube] セクションの cookies を更新\n"
                    "4. Save → アプリが自動再起動\n\n"
                    f"詳細: {err[-300:]}"
                )
            raise RuntimeError(
                "YouTube CDN 403エラー（IP制限）\n\n"
                "Streamlit CloudのIPがYouTube CDNにブロックされています。\n"
                "Streamlit Secrets の [youtube] セクションにcookiesを設定してください。\n\n"
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
