"""YouTube動画ダウンローダー (yt-dlp)"""
import subprocess
import json
from pathlib import Path


# クラウド環境でのyt-dlp共通オプション（403対策・JS runtime指定）
_YTDLP_BASE = [
    "--no-playlist",
    "--extractor-args", "youtube:player_client=web_creator,tv_embedded,default",
    "--add-header", "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "--no-check-certificates",
]

# Node.js が使える場合は JS runtime に指定
import shutil as _shutil
if _shutil.which("node"):
    _YTDLP_BASE += ["--js-runtimes", "node"]


def get_video_info(url: str) -> dict:
    """動画のメタ情報を取得"""
    result = subprocess.run(
        ["yt-dlp", "--dump-json"] + _YTDLP_BASE + [url],
        capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def download_video(url: str, output_dir: Path, progress_callback=None) -> Path:
    """YouTube動画をmp4でダウンロードして返す"""
    output_dir.mkdir(parents=True, exist_ok=True)

    # video_id取得
    id_result = subprocess.run(
        ["yt-dlp", "--print", "id"] + _YTDLP_BASE + [url],
        capture_output=True, text=True, check=True
    )
    video_id = id_result.stdout.strip()
    output_template = str(output_dir / f"{video_id}.%(ext)s")

    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", output_template,
    ] + _YTDLP_BASE + [url]
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
