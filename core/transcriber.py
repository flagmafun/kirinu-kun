"""
core/transcriber.py
ローカル動画ファイルを faster-whisper で文字起こしする。

返り値は get_transcript() と同じ形式:
    [{"start": float, "end": float, "text": str}, ...]

モデルサイズ: "small"（日本語精度と速度のバランス）
compute_type: "int8"（CPU向け最適化）
"""

from __future__ import annotations
from pathlib import Path


def transcribe_file(
    video_path: str | Path,
    language: str = "ja",
    model_size: str = "small",
) -> list[dict]:
    """
    動画/音声ファイルを faster-whisper で文字起こしして返す。

    Parameters
    ----------
    video_path : ファイルパス（動画/音声どちらでも可）
    language   : 言語コード（"ja" / "en" など、None で自動検出）
    model_size : "tiny" / "base" / "small" / "medium" / "large-v3"

    Returns
    -------
    list of {"start": float, "end": float, "text": str}
    """
    from faster_whisper import WhisperModel

    model = WhisperModel(
        model_size,
        device="cpu",
        compute_type="int8",
    )

    segments, _ = model.transcribe(
        str(video_path),
        language=language,
        beam_size=5,
        vad_filter=True,          # 無音区間をスキップして高速化
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

    return result
