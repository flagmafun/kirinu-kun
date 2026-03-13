FROM python:3.11-slim

# システム依存パッケージ（日本語フォント, Node.js 20, xz-utils）
# ★ ffmpeg は apt-get 版（5.1）だと H.264 Late SEI 未対応のため別途インストール
#    エラー例: "Late SEI is not implemented. Update your FFmpeg version to the newest one from Git."
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        curl \
        xz-utils \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# ffmpeg 最新静的ビルド (master/v7+) をインストール
# BtbN 静的ビルド: H.264 Late SEI 対応済み・全コーデック込み
RUN curl -fL \
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz" \
    | tar -xJ --strip-components=2 -C /usr/local/bin \
      --wildcards '*/bin/ffmpeg' '*/bin/ffprobe' \
    && ffmpeg -version | head -1 \
    && ffprobe -version | head -1

WORKDIR /app

# Python 依存パッケージ
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8501

CMD ["python", "start.py"]
