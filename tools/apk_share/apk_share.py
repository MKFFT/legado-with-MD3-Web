#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Legado 局域网 APK 分发。

个人快速迭代用：电脑打包完，手机扫码从局域网直接下载 APK，不再走
LocalSend / 文件管理器。

设计参考了本地 homebox 项目的做法：
- 只绑局域网可达地址 + 随机令牌路径，不知道令牌返回 404；
- 根路径给手机一个极简下载页，`latest.apk` 流式返回 APK；
- 电脑端另开一个二维码页，二维码内容就是手机下载地址。

和 homebox 的差异：默认长期运行，动态服务 `build/apk-output` 里最新的
APK。重新打包不需要重启服务，手机刷新页面即可拿到新包。

用法（一般通过 apk_share.bat 调用）：
  apk_share.py                 启动（已在跑则只打开二维码页）
  apk_share.py --stop          停止后台服务
  apk_share.py --list-hosts    列出候选局域网 IP
  apk_share.py --port 8899     指定端口（被占用时自动顺延）
  apk_share.py --file a.apk    固定分发某个 APK
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parents[1]
sys.path.insert(0, str(TOOL_DIR))

try:
    import qrcodegen  # noqa: E402  (vendored, MIT, see qrcodegen.py)
except ImportError:  # pragma: no cover - only breaks if the file is missing
    print("[ERROR] 缺少 tools/apk_share/qrcodegen.py", file=sys.stderr)
    raise SystemExit(1)

APP_TAG = "legado-apk-share"
DEFAULT_PORT = 8899
DEFAULT_OUT_DIR = REPO_ROOT / "build" / "apk-output"
STATE_DIR = REPO_ROOT / "build"
PORT_FILE = STATE_DIR / "apk-share.port"
TOKEN_FILE = STATE_DIR / "apk-share.token"

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_APK_RE = re.compile(r"^legado-(.+?)-(core|comic)-(debug|release|noR8)\.apk$", re.I)

_state_lock = threading.Lock()
_state = {"downloads": 0, "last_download": 0.0}


# ---------------------------------------------------------------------------
# 网络与文件辅助
# ---------------------------------------------------------------------------


def is_private_ip(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return False
    if any(n < 0 or n > 255 for n in nums):
        return False
    return (
        nums[0] == 10
        or (nums[0] == 192 and nums[1] == 168)
        or (nums[0] == 172 and 16 <= nums[1] <= 31)
    )


def ip_tier(ip: str) -> int:
    if ip.startswith("192.168."):
        return 0
    if ip.startswith("10."):
        return 1
    if ip.startswith("172."):
        try:
            if 16 <= int(ip.split(".")[1]) <= 31:
                return 2
        except (IndexError, ValueError):
            pass
    return 3


def default_route_ip() -> str | None:
    """不发包，让系统选源地址；比枚举网卡更接近手机实际能到的地址。"""
    for target in (("8.8.8.8", 80), ("223.5.5.5", 80)):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.3)
            s.connect(target)
            ip = s.getsockname()[0]
        except OSError:
            ip = None
        finally:
            s.close()
        if ip and not ip.startswith("127.") and is_private_ip(ip):
            return ip
    return None


def candidate_ips() -> list[str]:
    ips: set[str] = set()
    detected = default_route_ip()
    if detected:
        ips.add(detected)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.add(ip)
    except socket.gaierror:
        pass
    return sorted(ips, key=lambda value: (ip_tier(value), value))


def load_token() -> str:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if _TOKEN_RE.fullmatch(token):
            return token
    except OSError:
        pass
    token = secrets.token_hex(16)
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
    except OSError:
        pass
    return token


def newest_apk(out_dir: Path) -> Path | None:
    if not out_dir.is_dir():
        return None
    files: list[Path] = []
    for path in out_dir.glob("*.apk"):
        try:
            if path.is_file():
                files.append(path)
        except OSError:
            continue
    if not files:
        return None

    def mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return max(files, key=mtime)


def format_bytes(size: int) -> str:
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GB"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{max(size, 0) // 1024} KB"


def format_time(ts: float) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def apk_info(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    match = _APK_RE.match(path.name)
    return {
        "name": path.name,
        "size": st.st_size,
        "size_text": format_bytes(st.st_size),
        "mtime": st.st_mtime,
        "mtime_text": format_time(st.st_mtime),
        "mtime_ns": st.st_mtime_ns,
        "key": f"{st.st_mtime_ns}-{st.st_size}",
        "version": match.group(1) if match else "",
        "product": match.group(2) if match else "",
        "build": match.group(3) if match else "",
    }


def get_json(url: str, timeout: float = 0.6) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-store"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def running_port(preferred: int) -> int | None:
    ports: list[int] = []
    try:
        ports.append(int(PORT_FILE.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        pass
    ports.append(preferred)
    for port in dict.fromkeys(ports):
        if port <= 0 or port > 65535:
            continue
        data = get_json(f"http://127.0.0.1:{port}/api/ping", timeout=0.4)
        if data and data.get("app") == APP_TAG:
            return port
    return None


def qr_svg(text: str, pixel_size: int = 300, border: int = 4) -> str:
    qr = qrcodegen.QrCode.encode_text(text, qrcodegen.QrCode.Ecc.MEDIUM)
    size = qr.get_size()
    total = size + border * 2
    parts = [
        f'<rect width="{total}" height="{total}" fill="#ffffff"/>',
        '<path fill="#000000" d="',
    ]
    for y in range(size):
        run_start = -1
        for x in range(size):
            dark = qr.get_module(x, y)
            if dark and run_start < 0:
                run_start = x
            elif not dark and run_start >= 0:
                parts.append(
                    f"M{run_start + border},{y + border}h{x - run_start}v1h-{x - run_start}z"
                )
                run_start = -1
        if run_start >= 0:
            parts.append(
                f"M{run_start + border},{y + border}h{size - run_start}v1h-{size - run_start}z"
            )
    parts.append('"/></svg>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{pixel_size}" '
        f'height="{pixel_size}" viewBox="0 0 {total} {total}" '
        f'shape-rendering="crispEdges">{"".join(parts)}</svg>'
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "LegadoApkShare/1.0"

    out_dir: Path = DEFAULT_OUT_DIR
    fixed_apk: Path | None = None
    token: str = ""
    host: str = ""
    port: int = 0

    # ---- logging ---------------------------------------------------------
    def log_request(self, code="-", size="-"):
        if self.path.startswith("/api/info"):
            return
        print(f"[{time.strftime('%H:%M:%S')}] {self.command} {self.path} -> {code}")
        sys.stdout.flush()

    def log_error(self, fmt, *args):
        # 半途断开的下载很常见，不要把 BaseHTTPRequestHandler 的堆栈刷满屏
        pass

    # ---- helpers ---------------------------------------------------------
    def current_apk(self) -> Path | None:
        if self.fixed_apk is not None:
            try:
                return self.fixed_apk if self.fixed_apk.is_file() else None
            except OSError:
                return None
        return newest_apk(self.out_dir)

    def is_loopback(self) -> bool:
        addr = self.client_address[0]
        return addr in ("127.0.0.1", "::1") or addr.startswith("127.")

    def send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_json(self, data: dict, head_only: bool = False) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_bytes(
            HTTPStatus.OK, body, "application/json; charset=utf-8", head_only=head_only
        )

    def send_html(self, body: str, head_only: bool = False) -> None:
        self.send_bytes(
            HTTPStatus.OK,
            body.encode("utf-8"),
            "text/html; charset=utf-8",
            head_only=head_only,
        )

    def send_404(self, head_only: bool = False) -> None:
        self.send_bytes(
            HTTPStatus.NOT_FOUND,
            b"404 Not Found",
            "text/plain; charset=utf-8",
            head_only=head_only,
        )

    # ---- routing ---------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self.route(head_only=True)

    def do_GET(self) -> None:  # noqa: N802
        self.route(head_only=False)

    def route(self, head_only: bool) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        loopback = self.is_loopback()

        if path == "/api/ping":
            self.send_json({"app": APP_TAG, "port": self.port}, head_only)
            return

        if path == "/api/info":
            if not loopback:
                self.send_404(head_only)
                return
            self.send_json(self.info_payload(), head_only)
            return

        if path == "/api/shutdown":
            if not loopback:
                self.send_404(head_only)
                return
            self.send_json({"ok": True}, head_only)
            if not head_only:
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        if path == "/qr":
            if not loopback:
                self.send_404(head_only)
                return
            self.send_html(self.admin_page(), head_only)
            return

        if path == "/":
            if not loopback:
                self.send_404(head_only)
                return
            self.send_bytes(
                HTTPStatus.FOUND,
                b"",
                "text/plain; charset=utf-8",
                {"Location": "/qr"},
                head_only,
            )
            return

        prefix = f"/s/{self.token}"
        if path == prefix:
            self.send_bytes(
                HTTPStatus.FOUND,
                b"",
                "text/plain; charset=utf-8",
                {"Location": prefix + "/"},
                head_only,
            )
            return
        if path == prefix + "/":
            self.send_html(self.phone_page(), head_only)
            return

        if path == prefix + "/latest.apk":
            self.serve_apk(head_only)
            return

        if path == prefix + "/info.json":
            info = apk_info(self.current_apk())
            self.send_json(
                {
                    "app": APP_TAG,
                    "url": self.phone_url(),
                    "apk": info,
                    "downloads": _state["downloads"],
                },
                head_only,
            )
            return

        self.send_404(head_only)

    # ---- payloads --------------------------------------------------------
    def phone_url(self) -> str:
        return f"http://{self.host}:{self.port}/s/{self.token}/"

    def info_payload(self) -> dict:
        info = apk_info(self.current_apk())
        with _state_lock:
            downloads = _state["downloads"]
            last_download = _state["last_download"]
        payload = {
            "app": APP_TAG,
            "host": self.host,
            "port": self.port,
            "url": self.phone_url(),
            "downloads": downloads,
            "last_download": last_download,
            "last_download_text": format_time(last_download),
            "apk": info,
        }
        if info:
            payload.update(info)
        return payload

    def serve_apk(self, head_only: bool) -> None:
        apk = self.current_apk()
        if apk is None:
            self.send_404(head_only)
            return
        try:
            size = apk.stat().st_size
        except OSError:
            self.send_404(head_only)
            return

        ascii_name = apk.name.encode("ascii", "ignore").decode("ascii") or "legado.apk"
        disposition = (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(apk.name)}"
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/vnd.android.package-archive")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", disposition)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()

        if head_only:
            return

        try:
            with apk.open("rb") as src:
                shutil.copyfileobj(src, self.wfile, 256 * 1024)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        with _state_lock:
            _state["downloads"] += 1
            _state["last_download"] = time.time()
        print(
            f"[{time.strftime('%H:%M:%S')}] sent {apk.name} "
            f"({format_bytes(size)}) to {self.client_address[0]}"
        )
        sys.stdout.flush()

    def admin_page(self) -> str:
        info = apk_info(self.current_apk())
        url = self.phone_url()
        qr = qr_svg(url)
        if info:
            file_line = f"{info['name']}"
            meta_line = f"{info['size_text']} · 打包于 {info['mtime_text']}"
        else:
            file_line = "等待 build/apk-output 里出现 APK"
            meta_line = "先运行 apk_build.bat 打包；此页会自动刷新"
        return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Legado 局域网更新</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0;
         min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #f2f4f7; color: #1b1f24; }}
  .card {{ background: #fff; border-radius: 20px; padding: 28px 32px;
           box-shadow: 0 12px 40px rgba(0,0,0,.10); max-width: 520px; text-align: center; }}
  h1 {{ font-size: 22px; margin: 0 0 6px; }}
  .sub {{ color: #667085; font-size: 13px; margin-bottom: 18px; }}
  .qr {{ background: #fff; padding: 10px; display: inline-block; line-height: 0;
         border: 1px solid #eaecf0; border-radius: 12px; }}
  .file {{ font-weight: 600; margin-top: 16px; word-break: break-all; }}
  .meta {{ color: #667085; font-size: 13px; margin-top: 4px; }}
  .url {{ margin-top: 14px; font-size: 13px; word-break: break-all; color: #0b5fff; }}
  .stats {{ margin-top: 14px; font-size: 12px; color: #667085; }}
  .tip {{ margin-top: 16px; font-size: 12px; color: #667085; line-height: 1.7;
          border-top: 1px solid #eaecf0; padding-top: 12px; text-align: left; }}
  code {{ background: #f2f4f7; padding: 1px 5px; border-radius: 4px; }}
</style></head>
<body><div class="card">
  <h1>Legado 局域网更新</h1>
  <div class="sub">手机相机 / 浏览器扫码，直接下载安装</div>
  <div class="qr">{qr}</div>
  <div class="file" id="file">{file_line}</div>
  <div class="meta" id="meta">{meta_line}</div>
  <div class="url">{url}</div>
  <div class="stats">已下载 <b id="count">0</b> 次 · 最近 <span id="last">—</span></div>
  <div class="tip">
    手机和电脑需在同一 Wi-Fi；首次运行若 Windows 防火墙弹窗请选「允许」。<br>
    手机下载完点通知栏 / 浏览器下载项的 APK 安装；首次需允许「安装未知应用」。<br>
    重新打包后本页自动刷新，手机浏览器刷新下载页即可拿到新包。<br>
    停止服务：<code>apk_share.bat --stop</code>。
  </div>
</div>
<script>
async function refresh() {{
  try {{
    const r = await fetch('/api/info', {{cache: 'no-store'}});
    if (!r.ok) return;
    const j = await r.json();
    const name = j.name || '等待 build/apk-output 里出现 APK';
    document.getElementById('file').textContent = name;
    document.getElementById('meta').textContent =
      j.name ? (j.size_text + ' · 打包于 ' + j.mtime_text)
             : '先运行 apk_build.bat 打包；此页会自动刷新';
    document.getElementById('count').textContent = j.downloads ?? 0;
    document.getElementById('last').textContent = j.last_download_text || '—';
    document.title = j.name ? ('Legado 分享 · ' + j.name) : 'Legado 局域网更新';
  }} catch (e) {{}}
}}
refresh(); setInterval(refresh, 2000);
</script></body></html>"""

    def phone_page(self) -> str:
        info = apk_info(self.current_apk())
        if info:
            href = f"latest.apk?v={info['key']}"
            body = f"""
  <div class="file">{info['name']}</div>
  <div class="meta">{info['size_text']} · 打包于 {info['mtime_text']}</div>
  <p><a class="btn" href="{href}">下载 APK</a></p>
  <div class="tip">下载完成后点通知栏里的 APK 安装；首次需允许「安装未知应用」。</div>"""
        else:
            body = """
  <div class="file">还没有可下载的 APK</div>
  <div class="meta">请先在电脑上运行 apk_build.bat 打包，然后刷新本页</div>"""
        return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Legado 更新</title>
<style>
  body {{ font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0;
         min-height: 100vh; display: flex; align-items: center; justify-content: center;
         background: #f2f4f7; color: #1b1f24; }}
  .card {{ background: #fff; border-radius: 20px; padding: 28px 24px;
           box-shadow: 0 12px 40px rgba(0,0,0,.10); width: min(88vw, 420px); text-align: center; }}
  h1 {{ font-size: 20px; margin: 0 0 14px; }}
  .file {{ font-weight: 600; word-break: break-all; }}
  .meta {{ color: #667085; font-size: 13px; margin-top: 6px; }}
  .btn {{ display: inline-block; margin: 22px 0 12px; padding: 14px 40px;
          border-radius: 999px; background: #0b5fff; color: #fff; text-decoration: none;
          font-size: 17px; font-weight: 600; }}
  .tip {{ color: #667085; font-size: 12px; line-height: 1.7; }}
</style></head>
<body><div class="card">
  <h1>Legado 更新</h1>{body}
</div></body></html>"""


# ---------------------------------------------------------------------------
# 启动 / 停止
# ---------------------------------------------------------------------------


def bind_server(preferred_port: int) -> tuple[ThreadingHTTPServer, int]:
    last_error: OSError | None = None
    for port in range(preferred_port, preferred_port + 20):
        if port > 65535:
            break
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
            return server, port
        except OSError as exc:
            last_error = exc
            continue
    raise OSError(f"端口 {preferred_port}..{preferred_port + 19} 都不可用: {last_error}")


def open_admin(port: int, no_open: bool) -> None:
    if no_open:
        return
    url = f"http://127.0.0.1:{port}/qr"
    try:
        webbrowser.open(url)
    except Exception:
        pass


def print_banner(info: dict | None, host: str, port: int, token: str, out_dir: Path) -> None:
    print()
    print("=" * 62)
    print(" Legado 局域网 APK 分发")
    print("=" * 62)
    print(f" APK 目录 : {out_dir}")
    if info:
        print(f" 当前 APK : {info['name']}  ({info['size_text']})")
        print(f" 打包时间 : {info['mtime_text']}")
    else:
        print(" 当前 APK : （还没有，先运行 apk_build.bat）")
    print(f" 手机地址 : http://{host}:{port}/s/{token}/")
    print(f" 电脑二维码: http://127.0.0.1:{port}/qr")
    print()
    print(" 手机和电脑要在同一 Wi-Fi；Windows 防火墙弹窗请选「允许」。")
    print(" 停止：本窗口 Ctrl+C，或另开窗口运行 apk_share.bat --stop")
    print("=" * 62)
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Legado 局域网 APK 分发")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="起始端口（默认 8899）")
    parser.add_argument("--host", help="二维码里使用的本机 IP（默认自动选择）")
    parser.add_argument("--file", help="固定分发的 APK 路径（默认 build/apk-output 里最新的）")
    parser.add_argument("--dir", help="APK 目录（默认 build/apk-output）")
    parser.add_argument("--no-open", action="store_true", help="不自动打开二维码页")
    parser.add_argument("--stop", action="store_true", help="停止正在运行的分发服务")
    parser.add_argument("--list-hosts", action="store_true", help="列出候选局域网 IP")
    parser.add_argument("--timeout", type=int, default=0, help="自动关闭时间（分钟，0=不自动关）")
    args = parser.parse_args(argv)
    if os.environ.get("LEGADO_APK_SHARE_NO_OPEN") == "1":
        args.no_open = True

    if args.list_hosts:
        detected = default_route_ip()
        print(f"默认出口 IP: {detected or '未检测到'}")
        print("候选 IP:")
        for ip in candidate_ips():
            print(f"  {ip}")
        return 0

    if args.stop:
        port = running_port(args.port)
        if port is None:
            print("分发服务未在运行。")
            return 0
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/shutdown", timeout=2
            ):
                pass
        except (OSError, urllib.error.URLError) as exc:
            print(f"[WARN] 停止请求失败: {exc}")
        print(f"已停止端口 {port} 上的分发服务。")
        return 0

    out_dir = Path(args.dir).expanduser().resolve() if args.dir else DEFAULT_OUT_DIR
    fixed_apk: Path | None = None
    if args.file:
        fixed_apk = Path(args.file).expanduser().resolve()
        if not fixed_apk.is_file():
            print(f"[ERROR] APK 不存在: {fixed_apk}", file=sys.stderr)
            return 1

    running = running_port(args.port)
    if running is not None:
        admin_url = f"http://127.0.0.1:{running}/qr"
        print(f"分发服务已在运行: {admin_url}")
        open_admin(running, args.no_open)
        return 0

    host = args.host or default_route_ip() or (candidate_ips() or ["127.0.0.1"])[0]
    if host.startswith("127."):
        print("[WARN] 没有检测到局域网 IP，手机可能无法连接。可用 --host 指定。")

    token = load_token()
    Handler.out_dir = out_dir
    Handler.fixed_apk = fixed_apk
    Handler.token = token
    Handler.host = host

    try:
        server, port = bind_server(args.port)
    except OSError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    Handler.port = port
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PORT_FILE.write_text(str(port) + "\n", encoding="utf-8")
    except OSError:
        pass

    print_banner(apk_info(fixed_apk or newest_apk(out_dir)), host, port, token, out_dir)
    open_admin(port, args.no_open)

    if args.timeout > 0:
        timer = threading.Timer(args.timeout * 60, server.shutdown)
        timer.daemon = True
        timer.start()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        server.server_close()
        try:
            PORT_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())