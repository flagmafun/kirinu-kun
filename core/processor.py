"""ffmpeg で動画を Shorts 形式（9:16 / 1080×1920）に変換

レイアウト:
  ┌───────────────────┐
  │  タイトルバー      │  ← size_key に依存 (17〜25% of 1920)
  ├───────────────────┤
  │  元動画 (16:9)    │  ← 固定 1080×608 px
  ├───────────────────┤
  │  底部画像 / 色     │  ← 残り
  └───────────────────┘
"""
import re
import math
import textwrap
import subprocess
import json
import tempfile
from pathlib import Path

# ── 定数 ─────────────────────────────────────────────────────
VIDEO_W = 1080
VIDEO_H = 608   # 16:9 at 1080px wide  (1080 × 9/16 = 607.5 → 608)
CANVAS_W = 1080
CANVAS_H = 1920

# ── フォント候補（日本語対応 / 優先順）──────────────────────────
# UIプレビューは -apple-system,'Hiragino Sans' → ヒラギノ角ゴシックを優先
_FONT_CANDIDATES = [
    # Linux / Ubuntu (Streamlit Cloud) — fonts-noto-cjk パッケージ
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Bold.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    # Linux / Ubuntu (IPAフォント fallback)
    "/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf",
    "/usr/share/fonts/truetype/ipafont-gothic/ipagp.ttf",
    # macOS
    "/System/Library/Fonts/ヒラギノ角ゴシック W7.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/HelveticaNeue.ttc",
]


def _strip_emoji(text):
    """絵文字・記号をPillowフォントで描画できる文字のみに絞る"""
    result = []
    for char in text:
        cp = ord(char)
        if (
            0x1F300 <= cp <= 0x1FAFF  # Emoji & Pictographs
            or 0x2600 <= cp <= 0x27BF  # Misc Symbols & Dingbats
            or 0xFE00 <= cp <= 0xFE0F  # Variation Selectors
            or cp in (0x200D, 0x20E3)  # ZWJ / Combining Keycap
        ):
            continue
        result.append(char)
    return "".join(result).strip()


def _get_font(size):
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _hex_to_rgb(hex_color):
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _parse_gradient_stops(css):
    """
    CSS グラデーション文字列から [(RGB tuple, position 0.0–1.0), ...] を返す。
    "#7c3aed 55%" のようなパーセント位置をそのまま使用し、
    未指定分は等間隔補完する。
    """
    # #RRGGBB (%位置あり)
    raw = []
    for m in re.finditer(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\s*(\d+(?:\.\d+)?)%", css):
        hex_str = m.group(1)
        if len(hex_str) == 3:
            hex_str = "".join(c * 2 for c in hex_str)
        raw.append((_hex_to_rgb("#" + hex_str), float(m.group(2)) / 100.0))

    if not raw:
        # % なし → 色のみ抽出して等間隔割り当て
        for m in re.finditer(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b", css):
            hex_str = m.group(1)
            if len(hex_str) == 3:
                hex_str = "".join(c * 2 for c in hex_str)
            raw.append((_hex_to_rgb("#" + hex_str), None))

    if not raw:
        return [((124, 58, 237), 0.0), ((37, 99, 235), 1.0)]

    # 端点が未指定なら 0.0 / 1.0 を割り当て
    n = len(raw)
    result = list(raw)
    if result[0][1] is None:
        result[0] = (result[0][0], 0.0)
    if result[-1][1] is None:
        result[-1] = (result[-1][0], 1.0)

    # 中間の None を線形補完
    for i in range(1, n - 1):
        if result[i][1] is None:
            prev_i = max(j for j in range(i)     if result[j][1] is not None)
            next_i = min(j for j in range(i+1,n) if result[j][1] is not None)
            span   = (next_i - prev_i) or 1
            frac   = (i - prev_i) / span
            pos    = result[prev_i][1] + (result[next_i][1] - result[prev_i][1]) * frac
            result[i] = (result[i][0], pos)

    return result


def _lerp_color_stops(stops, t):
    """stops=[(RGB, position), ...] から t∈[0,1] の補間色を返す"""
    t = max(0.0, min(1.0, t))
    if len(stops) == 1:
        return stops[0][0]
    for i in range(len(stops) - 1):
        c0, p0 = stops[i]
        c1, p1 = stops[i + 1]
        if p0 <= t <= p1:
            span = max(p1 - p0, 1e-6)
            frac = (t - p0) / span
            return tuple(int(c0[j] + (c1[j] - c0[j]) * frac) for j in range(3))
    return stops[-1][0]


def _render_gradient_135deg(width, height, stops):
    """
    135度（左上→右下）グラデーション PIL Image(RGB) を返す。
    UIの linear-gradient(135deg, ...) に対応。
    numpy を使用して高速計算。
    """
    from PIL import Image
    try:
        import numpy as np
        diag = max(width + height - 1, 1)
        y_arr = np.arange(height, dtype=np.float32).reshape(-1, 1)
        x_arr = np.arange(width,  dtype=np.float32).reshape(1, -1)
        t = np.clip((x_arr + y_arr) / diag, 0.0, 1.0)

        r_buf = np.zeros((height, width), dtype=np.float32)
        g_buf = np.zeros_like(r_buf)
        b_buf = np.zeros_like(r_buf)

        n = len(stops)
        for i in range(n - 1):
            (c0r, c0g, c0b), p0 = stops[i]
            (c1r, c1g, c1b), p1 = stops[i + 1]
            span = max(p1 - p0, 1e-6)
            mask = (t >= p0) & (t < p1 if i < n - 2 else t <= 1.0)
            lt   = np.clip(np.where(mask, (t - p0) / span, 0.0), 0.0, 1.0)
            r_buf += np.where(mask, c0r + (c1r - c0r) * lt, 0.0)
            g_buf += np.where(mask, c0g + (c1g - c0g) * lt, 0.0)
            b_buf += np.where(mask, c0b + (c1b - c0b) * lt, 0.0)

        arr = np.stack([
            np.clip(r_buf, 0, 255).astype(np.uint8),
            np.clip(g_buf, 0, 255).astype(np.uint8),
            np.clip(b_buf, 0, 255).astype(np.uint8),
        ], axis=2)
        return Image.fromarray(arr, "RGB")

    except ImportError:
        # numpy なし: 行ごとに中心 t 値で近似
        from PIL import ImageDraw
        img_g = Image.new("RGB", (width, height))
        draw_g = ImageDraw.Draw(img_g)
        diag = max(width + height - 1, 1)
        for y in range(height):
            # 行中央の t 値で近似（横方向グラデーションを省略）
            t_c = (width / 2 + y) / diag
            col = _lerp_color_stops(stops, t_c)
            draw_g.line([(0, y), (width - 1, y)], fill=col)
        return img_g


def _render_gradient_90deg(width, height, stops):
    """
    90度（左→右）グラデーション PIL Image(RGB) を返す。
    UIの linear-gradient(90deg, ...) アクセントラインに対応。
    """
    from PIL import Image
    try:
        import numpy as np
        x_arr = np.arange(width, dtype=np.float32).reshape(1, -1)
        t = np.clip(x_arr / max(width - 1, 1), 0.0, 1.0)  # (1, W)

        r_buf = np.zeros((1, width), dtype=np.float32)
        g_buf = np.zeros_like(r_buf)
        b_buf = np.zeros_like(r_buf)

        n = len(stops)
        for i in range(n - 1):
            (c0r, c0g, c0b), p0 = stops[i]
            (c1r, c1g, c1b), p1 = stops[i + 1]
            span = max(p1 - p0, 1e-6)
            mask = (t >= p0) & (t < p1 if i < n - 2 else t <= 1.0)
            lt   = np.clip(np.where(mask, (t - p0) / span, 0.0), 0.0, 1.0)
            r_buf += np.where(mask, c0r + (c1r - c0r) * lt, 0.0)
            g_buf += np.where(mask, c0g + (c1g - c0g) * lt, 0.0)
            b_buf += np.where(mask, c0b + (c1b - c0b) * lt, 0.0)

        row = np.stack([
            np.clip(r_buf, 0, 255).astype(np.uint8),
            np.clip(g_buf, 0, 255).astype(np.uint8),
            np.clip(b_buf, 0, 255).astype(np.uint8),
        ], axis=2)[0]  # (W, 3)
        arr = np.tile(row, (height, 1, 1))  # (H, W, 3)
        return Image.fromarray(arr, "RGB")

    except ImportError:
        from PIL import ImageDraw
        img_g = Image.new("RGB", (width, height))
        draw_g = ImageDraw.Draw(img_g)
        for x in range(width):
            col = _lerp_color_stops(stops, x / max(width - 1, 1))
            draw_g.line([(x, 0), (x, height - 1)], fill=col)
        return img_g


def _parse_sub_color(css_rgba):
    """rgba(R,G,B,A) または #RRGGBB をRGBAタプルに変換"""
    m = re.match(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)", css_rgba.strip())
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        a = int(float(m.group(4) if m.group(4) else "1") * 255)
        return (r, g, b, a)
    hex_m = re.search(r"#([0-9a-fA-F]{6})", css_rgba)
    if hex_m:
        return _hex_to_rgb("#" + hex_m.group(1)) + (220,)
    return (255, 255, 255, 184)


def _draw_pattern(draw, pattern_key, x0, y0, x1, y1):
    """指定エリア内にパターンを描画（UIのCSS rgba alpha値に合わせた半透明白）"""
    w = x1 - x0
    h = y1 - y0

    # UIのCSSに合わせたパターン別アルファ値
    _alpha_map = {
        "dots_sm":       56,   # rgba(255,255,255,0.22)
        "dots":          46,   # rgba(255,255,255,0.18)
        "dots_lg":       51,   # rgba(255,255,255,0.20)
        "stripes_thin":  23,   # rgba(255,255,255,0.09)
        "stripes":       26,   # rgba(255,255,255,0.10)
        "stripes_thick": 33,   # rgba(255,255,255,0.13)
        "grid":          31,   # rgba(255,255,255,0.12)
        "diamond":       26,   # rgba(255,255,255,0.10)
        "wave":          38,   # rgba(255,255,255,0.15)
    }
    alpha = _alpha_map.get(pattern_key, 46)

    if pattern_key in ("dots_sm", "dots", "dots_lg"):
        step = {"dots_sm": 8, "dots": 14, "dots_lg": 22}[pattern_key]
        r    = {"dots_sm": 1, "dots": 2,  "dots_lg": 3}[pattern_key]
        for y in range(0, h, step):
            for x in range(0, w, step):
                draw.ellipse(
                    [x0 + x - r, y0 + y - r, x0 + x + r, y0 + y + r],
                    fill=(255, 255, 255, alpha),
                )
    elif pattern_key in ("stripes_thin", "stripes", "stripes_thick"):
        gap   = {"stripes_thin": 8,  "stripes": 12, "stripes_thick": 14}[pattern_key]
        thick = {"stripes_thin": 1,  "stripes": 2,  "stripes_thick": 5}[pattern_key]
        for i in range(-h, w + h, gap):
            draw.line(
                [(x0 + i, y0), (x0 + i + h, y1)],
                fill=(255, 255, 255, alpha), width=thick,
            )
    elif pattern_key == "grid":
        step = 18
        for y in range(0, h, step):
            draw.line([(x0, y0 + y), (x1, y0 + y)], fill=(255, 255, 255, alpha), width=1)
        for x in range(0, w, step):
            draw.line([(x0 + x, y0), (x0 + x, y1)], fill=(255, 255, 255, alpha), width=1)
    elif pattern_key == "diamond":
        step = 9
        for y in range(-step, h + step, step):
            for x in range(-step, w + step, step):
                pts = [
                    (x0 + x, y0 + y - step),
                    (x0 + x + step, y0 + y),
                    (x0 + x, y0 + y + step),
                    (x0 + x - step, y0 + y),
                ]
                draw.polygon(pts, outline=(255, 255, 255, alpha))
    elif pattern_key == "wave":
        step = 18
        amp  = 5
        for y in range(0, h, step):
            pts = []
            for x in range(w + 1):
                wy = y0 + y + int(amp * math.sin(x / 18 * 2 * math.pi))
                pts.append((x0 + x, wy))
            if len(pts) >= 2:
                draw.line(pts, fill=(255, 255, 255, alpha), width=1)


def create_frame_image(
    title,
    theme_key,
    size_key,
    pattern_key,
    themes,
    sizes,
    bottom_image_path=None,
    catchphrase="",
):
    """
    1080×1920 フレーム画像（JPEG）を生成して返す。
    UIプレビューと同じデザイン（135deg グラデーション・ヒラギノフォント）で描画する。

    上部 = タイトルバー（グラデーション + パターン + キャッチコピー + タイトル）
    中央 = 黒エリア（ffmpeg で 16:9 動画がオーバーレイされる）
    下部 = 底部画像 or テーマグラデーション

    Returns: (frame_path: Path, title_h: int)
    """
    from PIL import Image, ImageDraw

    # 絵文字を除去してフォントで確実に描画できるテキストにする
    title       = _strip_emoji(title)
    catchphrase = _strip_emoji(catchphrase)

    theme          = themes.get(theme_key, list(themes.values())[0])
    bg_stops       = _parse_gradient_stops(theme["bg"])       # [(RGB, pos), ...]
    accent_stops   = _parse_gradient_stops(theme["accent"])   # [(RGB, pos), ...]
    sub_color      = _parse_sub_color(theme.get("sub", "rgba(255,255,255,0.72)"))

    # ── フォントサイズ（CANVAS_W=1080px 基準 / UIの320px幅×3.375倍）────
    # UIの font: 18px/22px → 動画: 61/74px
    font_s_map   = {"small": 40, "medium": 51, "large": 61, "xlarge": 74}
    font_s       = font_s_map.get(size_key, 61)
    catch_font_s = int(font_s * 0.56)   # UIの10px/18px ≈ 0.56比率

    font       = _get_font(font_s)
    catch_font = _get_font(catch_font_s)

    # ── パディング（UIと同比率）────────────────────────────────
    pad_x    = int(CANVAS_W * 0.05)   # 54px（UI: 16/320*1080≈54）
    pad_top  = int(font_s * 0.88)     # 上パディング
    pad_bot  = int(font_s * 0.70)     # 下パディング
    line_h   = int(font_s * 1.45)     # 行高さ（CSS lh:1.45相当）
    accent_h = max(5, int(CANVAS_W * 0.004))  # アクセントライン厚（UI: 4px）

    # ── 実際の文字幅で折り返し文字数を計測 ───────────────────────
    try:
        _bb     = font.getbbox("あ")
        _char_w = max(1, _bb[2] - _bb[0])
    except Exception:
        _char_w = font_s
    effective_w = CANVAS_W - pad_x * 2
    max_chars   = max(5, int(effective_w / _char_w))

    lines = textwrap.wrap(title, max_chars)[:3]

    # ── キャッチコピーバッジ高さを事前計算 ──────────────────────
    catch_badge_h = 0
    if catchphrase:
        try:
            _cb = catch_font.getbbox("あ")
            _ch = max(1, _cb[3] - _cb[1])
        except Exception:
            _ch = catch_font_s
        _badge_py     = int(catch_font_s * 0.28)
        catch_badge_h = _ch + _badge_py * 2 + int(catch_font_s * 0.55)  # badge + 下マージン

    # ── タイトルバー高さを動的計算（UIと同じ方式）──────────────
    text_block_h = len(lines) * line_h
    title_h      = pad_top + catch_badge_h + text_block_h + pad_bot + accent_h
    title_h      = max(title_h, int(CANVAS_H * 0.18))  # 最小高さ確保

    bottom_y = title_h + VIDEO_H
    bottom_h = CANVAS_H - bottom_y

    # ── RGBA キャンバス（黒ベース） ────────────────────────────
    img  = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 255))
    _pil_images_to_close = [img]  # GC補助用リスト

    # ── タイトルバー: 135度グラデーション（UIと同じ方向）──────
    grad_img = _render_gradient_135deg(CANVAS_W, title_h, bg_stops)
    img.paste(grad_img, (0, 0))   # RGBをRGBAに直接ペースト（alpha=255で完全上書き）

    draw = ImageDraw.Draw(img)

    # ── パターンオーバーレイ ──────────────────────────────────
    _draw_pattern(draw, pattern_key, 0, 0, CANVAS_W, title_h)

    # ── アクセントライン（タイトルバー下端） UIの::after と同等 ──
    accent_img = _render_gradient_90deg(CANVAS_W, accent_h, accent_stops)
    img.paste(accent_img, (0, title_h - accent_h))
    draw = ImageDraw.Draw(img)  # paste 後に再取得

    # ── キャッチコピー pill badge ───────────────────────────────
    y_cur = pad_top
    if catchphrase:
        try:
            _cb  = catch_font.getbbox(catchphrase)
            ct_w = _cb[2] - _cb[0]
            ct_h = _cb[3] - _cb[1]
        except Exception:
            ct_w = len(catchphrase) * catch_font_s
            ct_h = catch_font_s

        badge_px = int(catch_font_s * 0.55)  # 水平パディング
        badge_py = int(catch_font_s * 0.28)  # 垂直パディング
        bx0 = pad_x
        by0 = y_cur
        bx1 = bx0 + ct_w + badge_px * 2
        by1 = by0 + ct_h + badge_py * 2
        badge_r = (by1 - by0) // 2

        # バッジ背景を別レイヤーでアルファ合成（UI: rgba(0,0,0,0.18) + border rgba(255,255,255,0.22)）
        badge_layer = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        badge_draw  = ImageDraw.Draw(badge_layer)
        try:
            badge_draw.rounded_rectangle(
                [bx0, by0, bx1, by1], radius=badge_r,
                fill=(0, 0, 0, 46),           # rgba(0,0,0,0.18)
                outline=(255, 255, 255, 56),   # rgba(255,255,255,0.22)
            )
        except AttributeError:
            badge_draw.rectangle(
                [bx0, by0, bx1, by1],
                fill=(0, 0, 0, 46),
                outline=(255, 255, 255, 56),
            )
        img  = Image.alpha_composite(img, badge_layer)
        draw = ImageDraw.Draw(img)

        # テキスト（影 + 本文）
        tx = bx0 + badge_px
        ty = by0 + badge_py
        sh = max(1, int(catch_font_s * 0.05))
        draw.text((tx + sh, ty + sh), catchphrase, font=catch_font, fill=(0, 0, 0, 80))
        draw.text((tx, ty), catchphrase, font=catch_font, fill=sub_color)

        y_cur += (by1 - by0) + int(catch_font_s * 0.55)

    # ── タイトルテキスト（影 + 本文）─────────────────────────
    for line in lines:
        sh = max(2, int(font_s * 0.04))
        draw.text((pad_x + sh, y_cur + sh), line, font=font, fill=(0, 0, 0, 100))
        draw.text((pad_x, y_cur), line, font=font, fill=(255, 255, 255, 255))
        try:
            _, _, _tw, _th = font.getbbox(line)
        except AttributeError:
            _tw, _th = font.getsize(line)
        y_cur += _th + int(font_s * 0.18)

    # ── 中央ビデオエリアは黒のまま（ffmpeg でオーバーレイ） ────

    # ── 下部エリア ─────────────────────────────────────────────
    img_rgb = img.convert("RGB")

    if bottom_image_path:
        bpath = Path(bottom_image_path)
        if bpath.exists():
            try:
                bi         = Image.open(str(bpath)).convert("RGB")
                bi_w, bi_h = bi.size
                new_h      = int(bi_h * CANVAS_W / bi_w)
                bi         = bi.resize((CANVAS_W, new_h), Image.LANCZOS)
                if new_h > bottom_h:
                    bi    = bi.crop((0, 0, CANVAS_W, bottom_h))
                    new_h = bottom_h
                paste_y = bottom_y + (bottom_h - new_h) // 2
                img_rgb.paste(bi, (0, paste_y))
            except Exception:
                pass

    if not (bottom_image_path and Path(bottom_image_path).exists()):
        # 下部: bg グラデーションを薄くして縦に描画（UIのフォールバック）
        draw_rgb = ImageDraw.Draw(img_rgb)
        for y in range(bottom_h):
            t   = 1.0 - y / max(bottom_h - 1, 1)
            col = _lerp_color_stops(bg_stops, t * 0.55)
            draw_rgb.line([(0, bottom_y + y), (CANVAS_W, bottom_y + y)], fill=col)

    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    img_rgb.save(tmp.name, "JPEG", quality=95)
    tmp.close()
    return Path(tmp.name), title_h


def get_video_dimensions(path):
    """(width, height, duration) を返す"""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    info   = json.loads(result.stdout)

    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    w   = int(video["width"])
    h   = int(video["height"])
    dur = float(info["format"].get("duration", 0))
    return w, h, dur


def _build_crop_filter(w, h):
    """元解像度から 9:16 クロップフィルター文字列を生成（フォールバック用）"""
    target_ratio = 9 / 16
    if w / h >= target_ratio:
        crop_w = int(h * 9 / 16)
        crop_h = h
        x = "(iw-%d)/2" % crop_w
        y = "0"
    else:
        crop_w = w
        crop_h = int(w * 16 / 9)
        x = "0"
        y = "(ih-%d)/2" % crop_h
    return "crop=%d:%d:%s:%s,scale=1080:1920:flags=lanczos" % (crop_w, crop_h, x, y)


def create_shorts(
    input_path,
    output_path,
    max_duration=58,
    start_sec=0,
    title="",
    theme_key="purple",
    size_key="large",
    pattern_key="none",
    themes=None,
    sizes=None,
    bottom_image_path=None,
    catchphrase="",
):
    """
    動画を Shorts 形式（9:16 / 1080×1920）に変換。

    レイアウト（themes が指定された場合）:
      上部 = タイトルデザインバー
      中央 = 元動画（16:9 / 1080×608）← 中央にそのまま配置
      下部 = 底部画像 or テーマグラデーション

    themes が None の場合: 従来の 9:16 クロップ（フォールバック）
    Returns: output_path
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    w, h, total_dur = get_video_dimensions(input_path)
    actual_dur = min(max_duration, max(1, total_dur - start_sec))

    # ── 診断ログ ────────────────────────────────────────────
    _fsize = Path(input_path).stat().st_size if Path(input_path).exists() else -1
    print(
        f"[CREATE_SHORTS] input={input_path} size={_fsize}B "
        f"dims={w}x{h} total_dur={total_dur:.1f}s "
        f"start_sec={start_sec}s actual_dur={actual_dur}s",
        flush=True,
    )

    frame_jpg = None
    try:
        if themes:
            # ── フレーム画像生成 ────────────────────────────────
            frame_jpg, title_h = create_frame_image(
                title, theme_key, size_key, pattern_key,
                themes, sizes or {}, bottom_image_path,
                catchphrase=catchphrase,
            )

            # 入力動画を 1080×608 に変換（アスペクト比を維持してクロップ）
            scale_crop = (
                "scale=%d:%d:flags=lanczos:force_original_aspect_ratio=increase,"
                "crop=%d:%d"
            ) % (VIDEO_W, VIDEO_H, VIDEO_W, VIDEO_H)

            # filter_complex:
            #   [0:v] = 背景フレーム画像（loop 1）
            #   [1:v] = 元動画をスケール
            #   overlay で title_h の位置に貼り付け
            fc = (
                "[1:v]%s,setsar=1[vid];"
                "[0:v][vid]overlay=0:%d[out]"
            ) % (scale_crop, title_h)

            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-framerate", "30", "-i", str(frame_jpg),  # [0] 背景
                # -err_detect ignore_err: H.264 Late SEI など未対応の SEI をスキップ
                # -fflags +genpts: フラグメント MP4 や不正な PTS を持つ動画に対する対策
                "-err_detect", "ignore_err",
                "-fflags", "+genpts",
                "-ss", str(start_sec), "-i", str(input_path),             # [1] 動画
                "-t", str(actual_dur),
                "-filter_complex", fc,
                "-map", "[out]",
                "-map", "1:a?",   # 音声なし動画でも失敗しない
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-x264-params", "no-mbtree=1:rc-lookahead=0:ref=1",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", "-r", "30",
                "-threads", "2",
                str(output_path),
            ]
        else:
            # フォールバック: 従来の 9:16 クロップ
            crop_filter = _build_crop_filter(w, h)
            cmd = [
                "ffmpeg", "-y",
                "-err_detect", "ignore_err",
                "-fflags", "+genpts",
                "-ss", str(start_sec), "-i", str(input_path),
                "-t", str(actual_dur),
                "-vf", crop_filter,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-x264-params", "no-mbtree=1:rc-lookahead=0:ref=1",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", "-r", "30",
                "-threads", "2",
                str(output_path),
            ]

        print(f"[CREATE_SHORTS] cmd: {' '.join(str(c) for c in cmd)}", flush=True)
        import tempfile as _tf
        with _tf.TemporaryFile() as _stderr_tmp:
            result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=_stderr_tmp)
            _rc = result.returncode
            if _rc != 0:
                _stderr_tmp.seek(0)
                err = _stderr_tmp.read().decode("utf-8", errors="replace")
                print(f"[CREATE_SHORTS] ffmpeg FAILED rc={_rc}", flush=True)
                print(f"[CREATE_SHORTS] ffmpeg stderr:\n{err[:3000]}", flush=True)
                # 先頭と末尾の両方を表示（中間が切れても原因が見える）
                if len(err) > 800:
                    err_display = err[:400] + "\n...\n" + err[-400:]
                else:
                    err_display = err
                raise RuntimeError(f"ffmpeg失敗 (rc={_rc}): {err_display}")
            else:
                print(f"[CREATE_SHORTS] ffmpeg 成功 rc=0 → {output_path}", flush=True)

    finally:
        if frame_jpg and Path(frame_jpg).exists():
            try:
                Path(frame_jpg).unlink()
            except Exception:
                pass

    return output_path
