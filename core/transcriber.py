"""
core/transcriber.py
ローカル動画ファイルを faster-whisper で文字起こしする。

返り値は get_transcript() と同じ形式:
    [{"start": float, "end": float, "text": str}, ...]

モデルサイズ: "tiny"（メモリ節約、約150MB）
compute_type: "int8"（CPU向け最適化）
"""

from __future__ import annotations
from pathlib import Path


def transcribe_file(
    video_path: str | Path,
    language: str = "ja",
    model_size: str = "tiny",
) -> list[dict]:
    """
    動画/音声ファイルを faster-whisper で文字起こしして返す。
    メモリ節約のため:
      1. ffmpeg で音声(mono 16kHz)を抽出してから Whisper に渡す
      2. beam_size=1 で推論メモリを削減
      3. 処理後にモデルを明示的に解放

    Parameters
    ----------
    video_path : ファイルパス（動画/音声どちらでも可）
    language   : 言語コード（"ja" / "en" など、None で自動検出）
    model_size : "tiny" / "base" / "small" / "medium" / "large-v3"

    Returns
    -------
    list of {"start": float, "end": float, "text": str}
    """
    import gc
    import subprocess
    import tempfile
    from faster_whisper import WhisperModel

    video_path = Path(video_path)

    # ── 音声抽出（動画をそのまま渡すより大幅にメモリ節約）──────────
    tmp_audio = None
    audio_path = str(video_path)  # フォールバック: 変換失敗時はそのまま渡す
    try:
        tmp_audio = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_audio.close()
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-vn",                    # 映像を除去
                "-ac", "1",               # モノラル
                "-ar", "16000",           # 16kHz（Whisper の最適サンプリングレート）
                "-f", "wav",
                tmp_audio.name,
            ],
            capture_output=True,
            timeout=300,
        )
        if result.returncode == 0:
            audio_path = tmp_audio.name
    except Exception:
        pass  # 変換失敗時は元ファイルをそのまま使う

    # ── Whisper 文字起こし ──────────────────────────────────────────
    model = None
    try:
        model = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
        )

        segments, _ = model.transcribe(
            audio_path,
            language=language,
            beam_size=1,              # メモリ節約（精度より速度・省メモリを優先）
            vad_filter=True,          # 無音区間をスキップ
            vad_parameters={"min_silence_duration_ms": 500},
        )

        result = []
        for seg in segments:
            text = seg.text.strip()
            if text:
                result.append({
                    "start": float(seg.start),
                    "end":   float(seg.end),
                    "text":  text,
                })
    finally:
        # モデルを明示的に解放
        del model
        gc.collect()
        # 一時音声ファイルを削除
        if tmp_audio is not None:
            try:
                Path(tmp_audio.name).unlink(missing_ok=True)
            except Exception:
                pass

    return result
