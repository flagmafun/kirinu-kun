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


_CREDS_DIR         = Path(__file__).parent.parent / "credentials"
_COOKIES_PATH      = _CREDS_DIR / "cookies.txt"
# yt-dlp キャッシュディレクトリ（OAuth2 トークンもここに保存される）
_YTDLP_CACHE_DIR   = _CREDS_DIR / "ytdlp-cache"
# yt-dlp が OAuth2 トークンを書くパス: {cache_dir}/youtube/oauth.json
_OAUTH2_TOKEN_PATH = _YTDLP_CACHE_DIR / "youtube" / "oauth.json"


def has_oauth2_token() -> bool:
    """OAuth2 トークンファイルが存在するか確認する。"""
    return _OAUTH2_TOKEN_PATH.exists() and _OAUTH2_TOKEN_PATH.stat().st_size > 0


def get_oauth2_token_json() -> "str | None":
    """OAuth2 トークンの JSON 文字列を返す（Supabase 保存用）。"""
    if not has_oauth2_token():
        return None
    return _OAUTH2_TOKEN_PATH.read_text(encoding="utf-8")


def restore_oauth2_token(json_str: str) -> None:
    """Supabase から復元した JSON 文字列をトークンファイルに書き込む。"""
    _OAUTH2_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OAUTH2_TOKEN_PATH.write_text(json_str, encoding="utf-8")


def start_oauth2_flow() -> "subprocess.Popen":
    """OAuth2 デバイス認証フローを開始して Popen を返す。

    返り値の Popen の stderr を行単位で読み取ることで
    認証 URL とコードを取得できる。プロセスが完了すると
    _OAUTH2_TOKEN_PATH にトークンが保存される。
    """
    _YTDLP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # --skip-download: 認証・メタデータ取得は行うが動画ファイルはダウンロードしない
    # --simulate は yt-dlp 2025+ で認証前に終了することがあるため使わない
    return subprocess.Popen(
        [
            "yt-dlp",
            "--username", "oauth2", "--password", "",
            "--cache-dir", str(_YTDLP_CACHE_DIR),
            "--no-playlist", "--skip-download",
            # 短い公開動画を使って認証フローを起動する
            "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # stderr と stdout をマージして読みやすくする
        text=True,
    )


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
    "sign in to confirm you're not a bot",  # 認証なしアクセス拒否（cookies なし or 期限切れ）
    "sign in to confirm",                   # 上記の短縮形マッチ
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


_NCHALLENGE_HINTS = (
    "n challenge solving failed",           # Node.js 起動後 solver スクリプトが失敗
    "a supported javascript runtime",       # Node.js 自体が見つからない
    "challenge solver script distribution", # solver スクリプト未インストール（旧形式）
    "yt-dlp-ejs",                           # yt-dlp 2026.x 新形式: "yt-dlp-ejs package is missing"
    "wiki/ejs",                             # EJS wiki URL が stderr にあれば確実
)


def _nchallenge_failed_in_stderr(stderr: str) -> bool:
    """n-challenge 解決失敗を検出する。

    n-challenge 失敗の副作用で "page needs to be reloaded" が stderr に出るため、
    _cookies_expired_in_stderr より先に呼ぶこと（優先順位が重要）。
    """
    return any(h in stderr.lower() for h in _NCHALLENGE_HINTS)


def _get_ytdlp_base(use_cookies: bool = True) -> list:
    """yt-dlp共通オプションを返す。

    優先順位:
      1. OAuth2 トークンあり → web クライアント + OAuth2（cookies 不要、長期有効）
      2. cookies あり        → web クライアント + cookies
      3. どちらもなし        → android_vr（n-challenge 不要、ただし SABR の影響あり）

    いずれの場合も --cache-dir を固定パスに指定して OAuth2 トークンを永続化する。
    """
    _ensure_netscape_cookies()
    _has_oauth2  = has_oauth2_token()
    _has_cookies = (
        use_cookies and _COOKIES_PATH.exists() and _COOKIES_PATH.stat().st_size > 0
    )

    opts = [
        "--no-playlist", "--no-check-certificates",
        "--cache-dir", str(_YTDLP_CACHE_DIR),   # OAuth2 トークン永続化に必須
    ]
    node_bin = _find_node_binary()
    js_runtimes = ["--js-runtimes", f"node:{node_bin}" if node_bin else "node"]

    if _has_oauth2:
        # OAuth2: cookies 不要。n-challenge は引き続き必要なので --js-runtimes も渡す
        opts += [
            "--extractor-args", "youtube:player_client=web",
            "--username", "oauth2", "--password", "",
        ] + js_runtimes
    elif _has_cookies:
        opts += [
            "--extractor-args", "youtube:player_client=web",
            "--cookies", str(_COOKIES_PATH),
        ] + js_runtimes
    else:
        # 認証なし: android_vr は n-challenge 不要
        opts += ["--extractor-args", "youtube:player_client=android_vr"]

    return opts


def get_video_info(url: str) -> dict:
    """動画のメタ情報を取得"""
    url = _clean_url(url)
    result = subprocess.run(
        ["yt-dlp", "--dump-json"] + _get_ytdlp_base() + [url],
        capture_output=True, text=True, check=True, timeout=60
    )
    return json.loads(result.stdout)


def download_video(url: str, output_dir: Path, progress_callback=None) -> Path:
    """YouTube動画をmp4でダウンロードして返す"""
    url = _clean_url(url)
    output_dir.mkdir(parents=True, exist_ok=True)

    base = _get_ytdlp_base()
    # OAuth2 または cookies があれば「認証あり」として扱う（フォールバック判定に使用）
    has_cookies = (
        has_oauth2_token()
        or (_COOKIES_PATH.exists() and _COOKIES_PATH.stat().st_size > 0)
    )

    # video_id 取得
    id_result = subprocess.run(
        ["yt-dlp", "--print", "id"] + base + [url],
        capture_output=True, text=True, timeout=60
    )
    if id_result.returncode != 0:
        _stderr = (id_result.stderr or id_result.stdout or "").strip()
        # n-challenge 失敗を cookies 失敗より先にチェック（優先順位が重要）
        if _nchallenge_failed_in_stderr(_stderr) or "sign in to confirm" in _stderr.lower():
            # EJS/sign-in 失敗 → android_vr でリトライ（n-challenge 不要、認証不要）
            _id_retry = subprocess.run(
                ["yt-dlp", "--print", "id",
                 "--no-playlist", "--no-check-certificates",
                 "--extractor-args", "youtube:player_client=android_vr",
                 url],
                capture_output=True, text=True, timeout=60,
            )
            if _id_retry.returncode == 0:
                id_result = _id_retry
            else:
                # android_vr も失敗 → ios でリトライ
                _id_retry2 = subprocess.run(
                    ["yt-dlp", "--print", "id",
                     "--no-playlist", "--no-check-certificates",
                     "--extractor-args", "youtube:player_client=ios",
                     url],
                    capture_output=True, text=True, timeout=60,
                )
                if _id_retry2.returncode == 0:
                    id_result = _id_retry2
                else:
                    import importlib.util as _ilu
                    _node = _find_node_binary() or "未検出"
                    _has_ejs = _ilu.find_spec("yt_dlp_ejs") is not None
                    raise RuntimeError(
                        f"ダウンロード失敗: n-challenge / 認証エラー\n\n"
                        f"🔧 診断情報: Node.js={_node}  yt-dlp-ejs={'✅' if _has_ejs else '❌'}\n\n"
                        + _COOKIES_UPDATE_MSG
                        + f"\n詳細: {_stderr[-400:]}"
                    )
        elif has_cookies and _cookies_expired_in_stderr(_stderr):
            raise RuntimeError(
                _COOKIES_UPDATE_MSG + f"\n詳細: {_stderr[-400:]}"
            )
        elif id_result.returncode != 0:
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

    try:
        # 2時間の絶対タイムアウト（UI側の5分ストール検知が先に発動するため余裕を持たせる）
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=7200)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "YouTube ダウンロードが2時間を超えたため中断しました。\n"
            "動画が非常に長いか、接続が極めて不安定です。"
        )
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")
        err_l = err.lower()

        # ── エラー種別を判定 ──────────────────────────────────────────
        _ejs_fail  = _nchallenge_failed_in_stderr(err)
        _is_403    = "http error 403" in err_l or "403: forbidden" in err_l
        _sign_in   = "sign in to confirm" in err_l
        _is_sabr   = "sabr" in err_l or "missing a url" in err_l

        print(
            f"[DL] プライマリ失敗 code={result.returncode} "
            f"ejs={_ejs_fail} sign_in={_sign_in} 403={_is_403} sabr={_is_sabr}",
            flush=True,
        )

        # ① SABR（cookies なしの場合の特有エラー）
        if _is_sabr:
            raise RuntimeError(
                "YouTube SABR エラー（非認証セッション）\n\n"
                "YouTube が SABR-only streaming を適用しています。\n"
                "有効な cookies を設定すると回避できます。\n\n"
                + _COOKIES_UPDATE_MSG
                + f"\n詳細: {err[-300:]}"
            )

        # ② 条件判定に関わらず常にフォールバックチェーンを試みる
        #    （エラー検出ロジックのミスマッチに依存しないための安全策）
        import importlib.util as _ilu
        _node    = _find_node_binary() or "未検出"
        _has_ejs = _ilu.find_spec("yt_dlp_ejs") is not None
        _err_web = err
        _node_bin = _find_node_binary()
        _js_opts  = ["--js-runtimes", f"node:{_node_bin}" if _node_bin else "node"]
        # 認証オプション:
        #   _auth_web   = web/mweb 用（OAuth2 または cookies）
        #   _auth_novid = android_vr/ios 用（OAuth2 のみ。cookies は NG: #12482）
        #     ★ ios/android + cookies は NG: cookies 非対応クライアントなので
        #        yt-dlp がスキップして "Only images available" になる。
        if has_oauth2_token():
            _auth_web   = [
                "--cache-dir", str(_YTDLP_CACHE_DIR),
                "--username", "oauth2", "--password", "",
            ]
            _auth_novid = _auth_web  # OAuth2 はクライアント非依存
        elif _COOKIES_PATH.exists() and _COOKIES_PATH.stat().st_size > 0:
            _auth_web   = ["--cookies", str(_COOKIES_PATH)]
            _auth_novid = []  # cookies は android_vr/ios に渡さない
        else:
            _auth_web   = []
            _auth_novid = []
        _fallbacks = [
            # mweb: モバイル web（n-challenge あり、cookies 対応）
            ("mweb",       ["--extractor-args", "youtube:player_client=mweb"]
                           + _js_opts + _auth_web),
            # android_vr: n-challenge 不要（cookies を渡さない）
            ("android_vr", ["--extractor-args", "youtube:player_client=android_vr"]
                           + _auth_novid),
            # ios: n-challenge 不要（cookies を渡さない）
            ("ios",        ["--extractor-args", "youtube:player_client=ios"]
                           + _auth_novid),
        ]
        _fb_errors: dict = {}
        for _fb_name, _fb_opts in _fallbacks:
            _fb_base = ["--no-playlist", "--no-check-certificates"] + _fb_opts
            _fb_cmd  = ["yt-dlp", "-f", fmt, "--merge-output-format", "mp4",
                        "-o", output_template] + _fb_base + [url]
            print(f"[DL] fallback {_fb_name} 試行中...", flush=True)
            try:
                _fb_r = subprocess.run(_fb_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3600)
            except subprocess.TimeoutExpired:
                _fb_errors[_fb_name] = "タイムアウト（10分超過）"
                continue
            if _fb_r.returncode == 0:
                print(f"[DL] fallback {_fb_name} 成功", flush=True)
                result = _fb_r
                err    = ""
                break
            _fb_errors[_fb_name] = _fb_r.stderr.decode("utf-8", errors="replace")
            print(f"[DL] fallback {_fb_name} 失敗: {_fb_errors[_fb_name][-200:]}", flush=True)
        else:
            # 全フォールバック失敗 → 原因別メッセージ
            _detail = f"\nweb: {_err_web[-300:]}"
            for _n, _e in _fb_errors.items():
                _detail += f"\n{_n}: {_e[-200:]}"
            if _ejs_fail:
                raise RuntimeError(
                    "YouTube ダウンロード失敗（EJS/n-challenge + 全フォールバック失敗）\n\n"
                    f"🔧 Node.js={_node}  yt-dlp-ejs={'✅' if _has_ejs else '❌'}\n\n"
                    "ios/android_vr も失敗した場合は cookies が期限切れの可能性があります。\n"
                    + _COOKIES_UPDATE_MSG
                    + _detail
                )
            elif _sign_in:
                raise RuntimeError(
                    "YouTube 認証エラー（Sign in to confirm you're not a bot）\n\n"
                    + _COOKIES_UPDATE_MSG
                    + _detail
                )
            elif _is_403:
                raise RuntimeError(
                    "YouTube ダウンロード失敗（CDN 403 + 全フォールバック失敗）\n\n"
                    + (
                        "PO Token 問題または cookies が期限切れの可能性があります。\n"
                        + _COOKIES_UPDATE_MSG
                        if has_cookies else
                        "IP がブロックされているか、cookies を設定してください。\n"
                    )
                    + _detail
                )
            else:
                raise RuntimeError(
                    f"YouTube ダウンロード失敗（全フォールバック失敗）\n"
                    + _detail
                )

        # フォールバックが成功した場合は result.returncode == 0 なので raise しない
        # （for...break 後もここに到達するため returncode を再チェック）
        if result.returncode != 0:
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
