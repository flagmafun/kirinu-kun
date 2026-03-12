"""YouTube動画ダウンローダー (yt-dlp)

クライアント選択戦略（Streamlit Cloud 向け）:
  Streamlit Cloud には Node.js がないため、web/mweb クライアントの
  n-challenge（CDN URL 署名）が解決できず動画フォーマットが取得できない。

  唯一動作するのは android_vr クライアント:
    - n-challenge 不要（ratebypass=yes）
    - cookies 不要（Android アプリ型 API）
    - PO Token 不要
    - ただし一部の動画でCDN 403（IP制限）が発生する場合あり

  cookies の使途:
    - ダウンロードには使えない（android_vr は cookies 非対応）
    - 有効性チェック用として保管しておく（将来対応クライアントが増えた場合）
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


def _get_ytdlp_base(use_cookies: bool = True) -> list[str]:
    """yt-dlp共通オプションを返す。
    Streamlit Cloud では常に android_vr（n-challenge不要）を使用。
    use_cookies 引数は互換性のため残しているが動作に影響しない。
    """
    _ensure_netscape_cookies()
    opts = ["--no-playlist", "--no-check-certificates"]
    # android_vr: n-challenge不要・cookies不要・PO Token不要
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
    """YouTube動画をmp4でダウンロードして返す（android_vrクライアント使用）"""
    url = _clean_url(url)
    output_dir.mkdir(parents=True, exist_ok=True)

    base = _get_ytdlp_base()

    # video_id 取得
    id_result = subprocess.run(
        ["yt-dlp", "--print", "id"] + base + [url],
        capture_output=True, text=True
    )
    if id_result.returncode != 0:
        _stderr = (id_result.stderr or id_result.stdout or "").strip()
        raise RuntimeError(
            f"yt-dlp --print id 失敗 (code {id_result.returncode}):\n{_stderr[-600:]}"
        )
    video_id = id_result.stdout.strip()
    output_template = str(output_dir / f"{video_id}.%(ext)s")

    # android_vr: 22(720p progressive) → 18(360p) → best の順で試す
    fmt = "22/18/best"

    cmd = ["yt-dlp", "-f", fmt, "--merge-output-format", "mp4",
           "-o", output_template] + base + [url]

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")
        if "HTTP Error 403" in err or "403: Forbidden" in err:
            raise RuntimeError(
                "YouTube CDN 403エラー（IP制限）\n\n"
                "Streamlit Cloud のIPがYouTube CDNにブロックされています。\n"
                "この動画はStreamlit Cloudからダウンロードできない可能性があります。\n\n"
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
) -> tuple[bool, str]:
    """
    YouTube へのアクセス可否と cookies ファイルの設定状況を確認。
    android_vr クライアントで軽量テスト（n-challenge不要・30秒タイムアウト）。
    Returns: (is_ok: bool, message: str)
    """
    _ensure_netscape_cookies()
    has_cookies = _COOKIES_PATH.exists() and _COOKIES_PATH.stat().st_size > 0

    # android_vr で YouTube アクセステスト
    try:
        result = subprocess.run(
            ["yt-dlp", "--print", "id", "--no-playlist", "--no-check-certificates",
             "--extractor-args", "youtube:player_client=android_vr", test_url],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "タイムアウト（30秒）"

    if result.returncode == 0 and result.stdout.strip():
        if has_cookies:
            return True, "✅ YouTube アクセス可能、cookies ファイルも設定されています"
        return True, "⚠️ YouTube アクセス可能ですが cookies が設定されていません"

    stderr = (result.stderr or result.stdout or "").strip()
    if "403" in stderr or "Forbidden" in stderr:
        if has_cookies:
            return False, "❌ YouTube CDN にブロックされています（IP制限）。cookies は設定されています。"
        return False, "❌ YouTube CDN にブロックされています（IP制限）"
    return False, f"❌ アクセス失敗: {stderr[-200:]}"
