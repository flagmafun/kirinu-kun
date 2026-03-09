"""YouTube動画ダウンローダー (yt-dlp / android_vr client)"""
import subprocess
import json
import re
from pathlib import Path


def _clean_url(url: str) -> str:
    """URLから markdown 記法などの余分な文字を取り除く"""
    url = url.strip()
    # 先頭の markdown 記号を除去（__url__ や _url_）
    url = re.sub(r'^[_*`"\']+', '', url)
    # URL を抽出（_ は URL 内で許可、ただし末尾の __ は markdown の終端なので除去）
    m = re.match(r'(https?://[^\s\'"<>`]+)', url)
    if not m:
        return url
    candidate = m.group(1)
    # 末尾の __ (markdown closing bold) だけを除去
    if candidate.endswith('__'):
        candidate = candidate[:-2]
    return candidate

_CREDS_DIR = Path(__file__).parent.parent / "credentials"
_COOKIES_PATH = _CREDS_DIR / "cookies.txt"


def _get_ytdlp_base() -> list[str]:
    """
    yt-dlp 共通オプションを返す。

    android_vr クライアント固定：
      - PO Token不要・n-challenge不要・Deno/Node不要
      - Streamlit Cloud等の環境でも動作
      - webクライアントはn-challengeにDeno/Nodeが必要なため使用しない
      - format 18（非DASH単一ファイル360p mp4）と組み合わせてCDN IP制限を回避

    cookies がある場合は追加で渡す（なくても動作する）。
    """
    has_cookies = _COOKIES_PATH.exists() and _COOKIES_PATH.stat().st_size > 0
    opts = ["--no-playlist", "--no-check-certificates"]

    # android_vr のみ使用（PO Token不要・n-challenge不要・Deno不要）
    # web クライアントはn-challengeにDeno/Nodeが必要 → Streamlit Cloudでは使えない
    opts += ["--extractor-args", "youtube:player_client=android_vr"]
    if has_cookies:
        opts += ["--cookies", str(_COOKIES_PATH)]

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

    # video_id取得
    id_result = subprocess.run(
        ["yt-dlp", "--print", "id"] + base + [url],
        capture_output=True, text=True, check=True
    )
    video_id = id_result.stdout.strip()
    output_template = str(output_dir / f"{video_id}.%(ext)s")

    cmd = [
        "yt-dlp",
        # format 18（itag=18, 360p 非DASH単一ファイルmp4）を最優先
        # Streamlit CloudのデータセンターIPはYouTube CDNにDASHストリームを403でブロックされるが
        # format 18 は単一HTTP URLでレンジリクエストを使わないためIP制限を受けにくい
        # fallback: ≤480p DASH（万一format 18がない場合）→ best
        "-f", "18/bestvideo[height<=480]+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", output_template,
    ] + base + [url]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"yt-dlp失敗 (code {result.returncode}): {err[-400:]}")

    # ダウンロード済みファイルを探す（拡張子不問でglobサーチ）
    for ext in [".mp4", ".mkv", ".webm", ".m4v", ".mov"]:
        path = output_dir / f"{video_id}{ext}"
        if path.exists():
            return path

    # glob fallback（ffmpegなし等で拡張子が変わる場合）
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
