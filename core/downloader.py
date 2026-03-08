"""YouTube動画ダウンローダー (yt-dlp)"""
import subprocess
import json
from pathlib import Path

_CREDS_DIR = Path(__file__).parent.parent / "credentials"
_COOKIES_PATH = _CREDS_DIR / "cookies.txt"


def _get_ytdlp_base() -> list[str]:
    """
    yt-dlp 共通オプションを返す。
    cookies.txt が存在すればそれを使用（最も信頼性が高い）。
    なければクライアント指定で対処。
    """
    opts = [
        "--no-playlist",
        "--no-check-certificates",
    ]
    if _COOKIES_PATH.exists() and _COOKIES_PATH.stat().st_size > 0:
        opts += ["--cookies", str(_COOKIES_PATH)]
    else:
        # cookies なし: web_safari が PO Token 不要で比較的安定
        opts += [
            "--extractor-args", "youtube:player_client=web_safari,ios,web_creator",
        ]
    return opts


def get_video_info(url: str) -> dict:
    """動画のメタ情報を取得"""
    result = subprocess.run(
        ["yt-dlp", "--dump-json"] + _get_ytdlp_base() + [url],
        capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def download_video(url: str, output_dir: Path, progress_callback=None) -> Path:
    """YouTube動画をmp4でダウンロードして返す"""
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
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
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
