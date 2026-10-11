"""反代产品（reverse-proxy）通用引擎 — 发现 / 健康检查 / 启动 / 暂停.

本模块不含任何具体反代产品的知识。每个反代项目在自己的根目录放一份
``relay.json`` 清单（名字、端口、启停命令、停止校验规则），控制台扫描
上级目录里所有兄弟项目的清单自动接入；新反代产品无需改控制台代码。

清单契约见 ../autoclaw-switch/README.md「relay.json（反代清单）」一节。
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CREATE_NO_WINDOW = 0x08000000
DEFAULT_WAIT_S = 15
ALERT_ERROR = "\u26a0"  # ⚠

if getattr(sys, "frozen", False):
    PROJECT_DIR = Path(sys.executable).resolve().parent
else:
    PROJECT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Relay:
    id: str
    name: str
    port: int
    host: str
    start_cmd: tuple[str, ...] | None
    start_cwd: str | None
    wait_seconds: int
    stop_match: str | None
    source: str

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


def _warn(msg: str) -> None:
    try:
        print(f"[relays] {msg}", file=sys.stderr)
    except Exception:
        pass


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def _scan_roots() -> list[Path]:
    """列出扫描根：其直接子目录里找 relay.json。

    覆盖三种布局：源码运行（项目目录）、exe 在项目目录、exe 在项目
    dist/ 子目录 —— 各取自身与两级父目录去重。另支持 controller.json
    的 relay_roots 显式覆盖（exe 被挪到别处时用）。
    """
    roots: list[Path] = []
    for cfg in (PROJECT_DIR / "controller.json",
                Path(sys.executable).resolve().parent / "controller.json"):
        if not cfg.is_file():
            continue
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception as e:
            _warn(f"{cfg.name} 解析失败: {e}")
            continue
        for r in data.get("relay_roots", []) or []:
            try:
                roots.append(Path(_expand(str(r))))
            except Exception:
                pass
    p = PROJECT_DIR
    for _ in range(3):
        roots.append(p)
        if p.parent == p:
            break
        p = p.parent
    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _parse_manifest(path: Path) -> Relay | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("清单必须是 JSON 对象")
        rid = str(data.get("id") or "").strip()
        name = str(data.get("name") or "").strip()
        port = int(data.get("port") or 0)
        if not rid or not name or not 0 < port < 65536:
            raise ValueError("id/name/port 无效")
        host = str(data.get("host") or "127.0.0.1").strip() or "127.0.0.1"
        health = data.get("health") or {}
        htype = str(health.get("type") or "tcp").lower()
        if htype != "tcp":
            raise ValueError(f"不支持的健康检查类型: {htype}")
        start = data.get("start") or {}
        cmd = start.get("cmd")
        start_cmd: tuple[str, ...] | None = None
        if isinstance(cmd, list) and cmd:
            start_cmd = tuple(_expand(str(c)) for c in cmd)
        cwd = start.get("cwd")
        start_cwd = _expand(str(cwd)) if cwd else None
        wait = int(start.get("wait_seconds") or DEFAULT_WAIT_S)
        stop = data.get("stop") or {}
        match = stop.get("cmdline_match")
        stop_match = str(match) if match else None
        if stop_match:
            re.compile(stop_match)  # 校验是合法正则
        return Relay(id=rid, name=name, port=port, host=host,
                     start_cmd=start_cmd, start_cwd=start_cwd,
                     wait_seconds=max(1, wait), stop_match=stop_match,
                     source=str(path))
    except Exception as e:
        _warn(f"{path} 清单无效，已跳过: {e}")
        return None


def discover_relays() -> list[Relay]:
    """扫描所有兄弟项目的 relay.json，按清单出现顺序返回（id 去重）。"""
    found: dict[str, Relay] = {}
    for root in _scan_roots():
        if not root.is_dir():
            continue
        try:
            manifests = sorted(root.glob("*/relay.json"))
        except OSError:
            continue
        for manifest in manifests:
            relay = _parse_manifest(manifest)
            if relay and relay.id not in found:
                found[relay.id] = relay
    return list(found.values())


def get_relay(rid: str) -> Relay | None:
    for relay in discover_relays():
        if relay.id == rid:
            return relay
    return None


def relay_online(r: Relay) -> bool:
    try:
        with socket.create_connection((r.host, r.port), timeout=1.5):
            return True
    except OSError:
        return False


def relay_state(r: Relay) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "host": r.host,
        "port": r.port,
        "address": r.address,
        "online": relay_online(r),
        "can_start": bool(r.start_cmd),
        "can_stop": bool(r.stop_match),
        "source": r.source,
    }


def start_relay(r: Relay) -> tuple[bool, str]:
    """执行清单声明的启动命令，随后等待端口就绪。返回 (ok, 消息)。"""
    if not r.start_cmd:
        return False, f"{r.name} 未声明启动命令（relay.json 缺 start.cmd）"
    try:
        # 输出重定向到临时文件而非管道：被启动的脚本可能用 close_fds=False
        # 再拉起常驻子进程，子进程会继承我们的管道写端，导致 communicate()
        # 永远等不到 EOF。文件重定向只等进程退出，不受孙子进程影响。
        with tempfile.TemporaryFile() as tf:
            subprocess.run(
                list(r.start_cmd),
                cwd=r.start_cwd or None,
                stdout=tf, stderr=subprocess.STDOUT, timeout=120,
                creationflags=CREATE_NO_WINDOW,
            )
            tf.seek(0)
            out = tf.read().decode("gbk", errors="replace").strip()
    except FileNotFoundError:
        return False, f"找不到启动程序: {r.start_cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, "启动命令 120s 超时"
    except Exception as e:
        return False, f"启动失败: {e}"
    deadline = time.monotonic() + r.wait_seconds
    while time.monotonic() < deadline:
        if relay_online(r):
            return True, out or f"{r.name} 已在线"
        time.sleep(0.5)
    return False, (out + f"\n{r.wait_seconds}s 内未监听 {r.address}").strip()


# PowerShell：找 LISTEN 端口的进程 → 校验命令行匹配清单声明的正则 → 才杀。
# 命令行不匹配（陌生进程占用了端口）时拒绝操作；端口没人听则幂等返回。
_STOP_PS = (
    "$c = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue "
    "| Select-Object -First 1; "
    "if (-not $c) { Write-Output 'not-listening'; exit 0 }; "
    "$p = Get-CimInstance Win32_Process -Filter \"ProcessId=$($c.OwningProcess)\"; "
    "if ($p.CommandLine -match '{match}') { "
    "Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; "
    "Write-Output \"stopped $($p.ProcessId)\" "
    "} else { Write-Output \"foreign:$($p.ProcessId):$($p.Name)\" }"
)


def stop_relay(r: Relay) -> tuple[bool, str]:
    """按清单声明的停止策略暂停反代。返回 (ok, 消息)。"""
    if not r.stop_match:
        return False, f"{r.name} 未声明停止策略（relay.json 缺 stop.cmdline_match）"
    ps = _STOP_PS.replace("{port}", str(r.port)).replace(
        "{match}", r.stop_match.replace("'", "''"))
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        out = ((proc.stdout or b"") + (proc.stderr or b"")).decode(
            "gbk", errors="replace").strip()
    except Exception as e:
        return False, f"停止失败: {e}"
    if out.startswith("stopped"):
        return True, f"已停止 {r.name} 进程 {out.split()[-1]}"
    if out == "not-listening":
        return True, f"{r.name} 本来就没在运行"
    if out.startswith("foreign:"):
        return False, (f"端口 {r.port} 被其他进程占用，已拒绝停止："
                       + out[len("foreign:"):])
    return False, f"停止失败: {out or '无输出'}"
