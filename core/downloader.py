"""YouTube動画ダウンローダー (yt-dlp)

クライアント選択戦略:

  [Cookie あり]  web + cookies
    - web クライアント: cookies 対応（REQUIRE_JS_PLAYER=True）
    - n-challenge は yt-dlp 2026 EJS システムが Node.js を使って解決
    - nodejs-wheel がバイナリを持つが PATH に出ないため、
      _setup_js_runtime() で site-packages/nodejs_wheel/bin/node を探して
      os.environ["PATH"] に追加する → yt-dlp subprocess が継承して発見
    - cookies による認証済みセッション → SABR 実験を回避
      （SABR は非認証セッションに強制適用される: yt-dlp/yt-dlp#12482）
    ★ ios / android + cookies は NG:
      これらのクライアントは cookies 非対応のため yt-dlp がスキップし
      フォールバック先が画像のみになる（"Only images available"）
    ★ web + player_skip=js は NG:
      player_skip=js はフォーマット抽出まで無効化するため同様に失敗

  [Cookie なし]  android_vr（最終手段）
    - n-challenge 不要・cookies 不要
    - 非認証のため SABR が適用される場合があり 403 になることも
"""
import os
import shutil
import subprocess
import json
import re
from pathlib import Path


# ── JS ランタイム（yt-dlp EJS n-challenge 解決用）────────────────────────────

def _find_node_binary() -> str:
    """node バイナリの絶対パスを返す。見つからなければ空文字。

    nodejs-wheel は site-packages/nodejs_wheel/bin/node にバイナリを置くが
    console_scripts エントリポイントを作らないため venv/bin に node が現れない。
    Python API 経由でパッケージディレクトリを特定して探す。
    """
    # 1. すでに PATH にある場合はそのまま使う
    node = shutil.which("node")
    if node:
        return node

    # 2. nodejs_wheel パッケージディレクトリから探す
    try:
        import nodejs_wheel as _nw
        pkg_dir = Path(_nw.__file__).parent
        for rel in ("bin/node", "node", ".bin/node"):
            p = pkg_dir / rel
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)
    except ImportError:
        pass

    # 3. site-packages 以下を直接検索（フォールバック）
    try:
        import site
        dirs = []
        try:
            dirs += site.getsitepackages()
        except AttributeError:
            pass
        try:
            dirs.append(site.getusersitepackages())
        except AttributeError:
            pass
        for sp in dirs:
            for rel in ("nodejs_wheel/bin/node", "nodejs/bin/node"):
                p = Path(sp) / rel
                if p.is_file() and os.access(p, os.X_OK):
                    return str(p)
    except Exception:
        pass

    return ""


def _setup_js_runtime() -> None:
    """nodejs-wheel の node を PATH に追加して yt-dlp EJS が使えるようにする。

    モジュール import 時に一度だけ呼び出す。
    設定後は subprocess で起動した yt-dlp が PATH を継承して node を発見できる。
    """
    node = _find_node_binary()
    if not node:
        return
    node_dir = str(Path(node).parent)
    current = os.environ.get("PATH", "")
    if node_dir not in current.split(os.pathsep):
        os.environ["PATH"] = node_dir + os.pathsep + current


_setup_js_runtime()  # モジュール import 時に一度だけ実行


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

    Cookieあり:   web クライアント + cookies
                  n-challenge は jsinterp（組み込み）または nodejs-wheel で解決
    Cookieなし:   android_vr（n-challenge 不要、ただし SABR の影響あり）
    """
    _ensure_netscape_cookies()
    has_cookies = use_cookies and _COOKIES_PATH.exists() and _COOKIES_PATH.stat().st_size > 0
    opts = ["--no-playlist", "--no-check-certificates"]

    if has_cookies:
        # web クライアント + cookies:
        #   - cookies 対応（ios/android は cookies 非対応で yt-dlp がスキップしてしまう）
        #   - n-challenge: nodejs-wheel の node バイナリを --js-runtimes で明示指定
        #   - 認証済みセッションにより SABR を回避
        #   ※ player_skip=js は NG（フォーマット抽出まで壊れる）
        opts += [
            "--extractor-args", "youtube:player_client=web",
            "--cookies", str(_COOKIES_PATH),
        ]
        # EJS n-challenge 解決のため --js-runtimes node を必ず渡す
        # （デフォルト 'deno' を上書きしないと deno 未インストール環境で失敗する）
        # node_bin あり → yt-dlp がそのパスを直接使用（PATH 検索不要）
        # node_bin なし → yt-dlp 自身が sysconfig.scripts + PATH で node を発見
        #   nodejs-wheel は console_scripts に node を登録するため
        #   venv/bin/node → 実バイナリへのラッパーとして機能する
        node_bin = _find_node_binary()
        opts += ["--js-runtimes", f"node:{node_bin}" if node_bin else "node"]
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
    cookies の有効性を確認（web クライアントで --print id テスト）。
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
