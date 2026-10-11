"""Controller Web UI — CC Switch 高级感控制面板（本地 Web 版）.

从 cc_switch_launcher 导入逻辑层（DB 读取 / 切换 / 连通检测），反代由 relays 引擎按清单管理，
本文件只负责:
  1. 标准库 HTTP 服务 (127.0.0.1:18777) 提供 API + controller.html
  2. 用 Edge --app 模式把页面开成无边框桌面窗口

零第三方依赖。用法:
    python webui.py          # 启动 Web UI（默认；打包后由 控制器.exe 调用）
    python webui.py --tk     # 回退到旧版 tkinter 界面
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import cc_switch_launcher as core
import relays

HOST = "127.0.0.1"
PORT = 18777
if getattr(sys, "frozen", False):
    HTML_PATH = Path(sys._MEIPASS) / "controller.html"
else:
    HTML_PATH = Path(__file__).with_name("controller.html")

APP_TYPES = [
    ("claude", "Claude Code（终端）", "🌙"),
    ("claude-desktop", "Claude Desktop（客户端）", "🖥"),
    ("codex", "Codex", "⚡"),
]

# switch 操作串行化（SQLite + live 文件写入不并发）
_switch_lock = threading.Lock()


# ---------- state ----------

def build_state() -> dict[str, Any]:
    apps: list[dict[str, Any]] = []
    for app_type, label, emoji in APP_TYPES:
        error: str | None = None
        try:
            providers = core.list_providers(app_type)
        except Exception as e:  # sqlite3.OperationalError 等
            providers = []
            error = str(e)
        items = []
        for p in providers:
            items.append({
                "id": p.id,
                "name": p.name,
                "current": p.is_current,
                "category": p.category,
                "notes": p.notes,
                "icon": p.icon,
                "iconColor": p.icon_color,
                "baseUrl": core.resolve_base_url(p),
            })
        apps.append({
            "id": app_type,
            "label": label,
            "emoji": emoji,
            "providers": items,
            "error": error,
        })
    return {
        "apps": apps,
        "relays": [relays.relay_state(r) for r in relays.discover_relays()],
    }


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "Controller/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
        pass

    # -- helpers --

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # -- routes --

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                body = HTML_PATH.read_bytes()
            except OSError:
                self._send(404, b"controller.html not found", "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                self._json(build_state())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        body = self._read_json()
        app_type = str(body.get("app") or "")
        provider_id = str(body.get("id") or "")

        try:
            if path == "/api/state":
                self._json(build_state())

            elif path == "/api/switch":
                if app_type not in ("claude", "codex"):
                    self._json({"ok": False, "error": f"不支持的应用: {app_type}"}, 400)
                    return
                if not provider_id:
                    self._json({"ok": False, "error": "缺少 id"}, 400)
                    return
                with _switch_lock:
                    if app_type == "claude":
                        core.switch_claude(provider_id)
                    else:
                        core.switch_codex(provider_id)
                self._json({"ok": True})

            elif path == "/api/test":
                providers = core.list_providers(app_type)
                target = next((p for p in providers if p.id == provider_id), None)
                if target is None:
                    self._json({"ok": False, "error": "找不到供应商"}, 404)
                    return
                self._json({"ok": True, "result": core.check_provider(target)})

            elif path in ("/api/relay/start", "/api/relay/stop"):
                rl = relays.get_relay(str(body.get("id") or ""))
                if rl is None:
                    self._json({"ok": False, "error": "找不到该反代产品"}, 404)
                    return
                if path.endswith("/start"):
                    ok, msg = relays.start_relay(rl)
                else:
                    with _switch_lock:
                        ok, msg = relays.stop_relay(rl)
                    time.sleep(0.3)
                self._json({"ok": ok, "message": msg,
                            "online": relays.relay_online(rl)})

            elif path == "/api/provider/add":
                if app_type not in ("codex", "claude", "claude-desktop"):
                    self._json({"ok": False,
                                "error": "目前仅支持 Codex / Claude / Claude Desktop"}, 400)
                    return
                with _switch_lock:
                    pid = core.add_provider(body)
                self._json({"ok": True, "id": pid})

            elif path == "/api/provider/models":
                ids = core.fetch_model_ids(str(body.get("baseUrl") or ""),
                                           str(body.get("apiKey") or ""))
                self._json({"ok": True, "models": ids})

            elif path == "/api/open-cc-switch":
                hwnd = core._cc_switch_window()
                if hwnd and core._activate_window(hwnd):
                    self._json({"ok": True, "message": "CC Switch 已在运行，已置前"})
                    return
                if not core.CC_SWITCH_EXE.exists():
                    self._json({"ok": False,
                                "error": f"找不到 CC Switch: {core.CC_SWITCH_EXE}"}, 404)
                    return
                os.startfile(str(core.CC_SWITCH_EXE))  # type: ignore[attr-defined]
                self._json({"ok": True, "message": "已启动 CC Switch"})

            elif path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)),
                                 daemon=True).start()

            else:
                self._json({"ok": False, "error": "not found"}, 404)

        except core.SwitchError as e:
            self._json({"ok": False, "error": str(e)}, 409)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)


# ---------- Edge app window ----------

EDGE_PATHS = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe")),
]


def open_window(url: str) -> None:
    for p in EDGE_PATHS:
        if p.exists():
            try:
                subprocess.Popen([str(p), f"--app={url}", "--window-size=1040,800"])
                return
            except Exception:
                break
    webbrowser.open(url)


def port_in_use() -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((HOST, PORT)) == 0


def main() -> int:
    if "--tk" in sys.argv:
        return core.main()

    url = f"http://{HOST}:{PORT}/"
    if port_in_use():
        # 已有实例在跑 → 只把窗口拉起来
        open_window(url)
        return 0

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.4)
    open_window(url)
    print(f"[controller] web ui: {url}  (Ctrl+C 退出)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
