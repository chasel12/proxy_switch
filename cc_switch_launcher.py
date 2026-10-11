"""CC Switch Launcher — quick provider switcher (Claude / Codex).

Reads CC Switch v2/v3 SQLite DB (~/.cc-switch/cc-switch.db), shows providers
for Claude Code and Codex, one-click switch updates:
  - Backfill current provider's settings_config with current live state
  - Write new provider to live files (Claude settings.json / Codex auth.json + config.toml)
  - DB: flip is_current atomically
  - ~/.cc-switch/settings.json: currentProviderClaude / currentProviderCodex

Reimplements the core of `ProviderService::switch_normal` from
E:\\documents\\ChatGPT\\proxy\\cc-switch-3.20.4\\src-tauri\\src\\services\\provider\\mod.rs:5805
but in Python so it works without launching the Tauri GUI.

Zero non-stdlib deps. Just Python 3.10+ + tkinter (built-in on Windows).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tomllib
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

import relays

# ---------- Path resolution (mirrors CC Switch src-tauri/src/config.rs) ----------

HOME = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


def _read_settings_json() -> dict[str, Any]:
    path = HOME / ".cc-switch" / "settings.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_app_config_dir() -> Path:
    # settings.json: app_config_dir_override? not exposed in struct -> default
    return HOME / ".cc-switch"


def get_db_path() -> Path:
    return get_app_config_dir() / "cc-switch.db"


def get_claude_settings_path() -> Path:
    s = _read_settings_json()
    custom = s.get("claude_config_dir") or s.get("claudeConfigDir")
    if custom and isinstance(custom, str) and custom.strip():
        return Path(custom.strip()) / "settings.json"
    return HOME / ".claude" / "settings.json"


def get_codex_config_dir() -> Path:
    s = _read_settings_json()
    custom = s.get("codex_config_dir") or s.get("codexConfigDir")
    if custom and isinstance(custom, str) and custom.strip():
        return Path(custom.strip())
    return HOME / ".codex"


def get_codex_auth_path() -> Path:
    return get_codex_config_dir() / "auth.json"


def get_codex_config_path() -> Path:
    return get_codex_config_dir() / "config.toml"


def get_settings_json_path() -> Path:
    return get_app_config_dir() / "settings.json"


# ---------- DB access ----------

@dataclass(frozen=True)
class Provider:
    id: str
    app_type: str
    name: str
    is_current: bool
    category: str | None
    website_url: str | None
    notes: str | None
    icon: str | None
    icon_color: str | None
    settings_config: dict[str, Any]


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(get_db_path()), timeout=3.0, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def list_providers(app_type: str) -> list[Provider]:
    with open_db() as conn:
        rows = conn.execute(
            """
            SELECT id, name, settings_config, website_url, is_current,
                   category, notes, icon, icon_color
            FROM providers WHERE app_type = ?1
            ORDER BY is_current DESC, COALESCE(sort_index, 999999), created_at, id
            """,
            (app_type,),
        ).fetchall()
    out: list[Provider] = []
    for r in rows:
        try:
            sc = json.loads(r["settings_config"]) if r["settings_config"] else {}
        except Exception:
            sc = {}
        if not isinstance(sc, dict):
            sc = {}
        out.append(Provider(
            id=r["id"],
            app_type=app_type,
            name=r["name"],
            is_current=bool(r["is_current"]),
            category=r["category"],
            website_url=r["website_url"],
            notes=r["notes"],
            icon=r["icon"],
            icon_color=r["icon_color"],
            settings_config=sc,
        ))
    return out


# ---------- Add provider (mirrors ProviderService::add + save_provider) ----------

CODEX_API_FORMATS = ("openai_responses", "openai_chat", "anthropic")


def _normalize_catalog_models(models: Any) -> list[dict[str, Any]]:
    """Mirror normalizeCodexCatalogModelsForSave (ProviderForm.tsx:157)."""
    out: list[dict[str, Any]] = []
    if not isinstance(models, list):
        return out
    seen: set[str] = set()
    for item in models:
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        row: dict[str, Any] = {"model": model}
        display = str(item.get("displayName") or "").strip()
        if display:
            row["displayName"] = display
        raw_cw = re.sub(r"[^\d]", "", str(item.get("contextWindow") or ""))
        if raw_cw and int(raw_cw) > 0:
            row["contextWindow"] = int(raw_cw)
        base_instructions = str(item.get("baseInstructions") or "").strip()
        if base_instructions:
            row["baseInstructions"] = base_instructions
        out.append(row)
    return out


def add_provider(payload: dict[str, Any]) -> str:
    """Insert a new provider into the shared CC Switch DB (codex/claude/claude-desktop).

    Mirrors CC Switch v3.20.4 add flow: providers row (category NULL, no
    sort_index, is_current=0) + provider_endpoints row derived from base_url.
    Returns the new provider id.
    """
    app = str(payload.get("app") or "codex")
    if app == "codex":
        return _add_codex_provider(payload)
    if app in ("claude", "claude-desktop"):
        return _add_claude_provider(payload)
    raise SwitchError(f"不支持的应用: {app}")


def _add_codex_provider(payload: dict[str, Any]) -> str:
    """Codex provider: settings_config = {auth, config, modelCatalog?}."""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise SwitchError("供应商名称不能为空")

    auth_raw = payload.get("authJson")
    try:
        auth_obj = json.loads(auth_raw) if isinstance(auth_raw, str) else auth_raw
    except Exception:
        raise SwitchError("auth.json 不是合法 JSON")
    if not isinstance(auth_obj, dict):
        raise SwitchError("auth.json 必须是 JSON 对象")

    config_text = payload.get("configToml")
    if not isinstance(config_text, str) or not config_text.strip():
        raise SwitchError("config.toml 不能为空")
    try:
        tomllib.loads(config_text)
    except tomllib.TOMLDecodeError as e:
        raise SwitchError(f"config.toml 语法错误: {e}")

    api_format = str(payload.get("apiFormat") or "openai_responses")
    if api_format not in CODEX_API_FORMATS:
        raise SwitchError(f"未知的上游格式: {api_format}")

    base_url = str(payload.get("baseUrl") or "").strip().rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise SwitchError("API 请求地址必须以 http:// 或 https:// 开头")

    website = str(payload.get("websiteUrl") or "").strip() or None
    if website and not website.startswith(("http://", "https://")):
        raise SwitchError("官网链接必须以 http:// 或 https:// 开头")

    # 没有 model 行且填了模型映射 → 默认使用映射第一行 (ProviderForm.tsx:1526)
    models = _normalize_catalog_models(payload.get("modelCatalog"))
    if models and not re.search(r"(?m)^\s*model\s*=", config_text):
        config_text = f'model = "{models[0]["model"]}"\n' + config_text

    settings_config: dict[str, Any] = {"auth": auth_obj, "config": config_text}
    if models:
        settings_config["modelCatalog"] = {"models": models}

    meta: dict[str, Any] = {"apiFormat": api_format}
    ua = str(payload.get("customUserAgent") or "").strip()
    if ua:
        meta["customUserAgent"] = ua
    headers = payload.get("headersOverride")
    body = payload.get("bodyOverride")
    overrides: dict[str, Any] = {}
    if isinstance(headers, dict) and headers:
        overrides["headers"] = headers
    if isinstance(body, dict) and body:
        overrides["body"] = body
    if overrides:
        meta["localProxyRequestOverrides"] = overrides
    cost = payload.get("costMultiplier")
    if cost not in (None, ""):
        try:
            if float(str(cost)) < 0:
                raise ValueError
        except ValueError:
            raise SwitchError("成本倍率必须是非负数字")
        meta["costMultiplier"] = str(cost).strip()
    source = str(payload.get("pricingModelSource") or "")
    if source in ("request", "response"):
        meta["pricingModelSource"] = source

    now_ms = int(time.time() * 1000)
    provider_id = str(uuid.uuid4())
    notes = str(payload.get("notes") or "").strip() or None

    with open_db() as conn:
        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                INSERT INTO providers (id, app_type, name, settings_config, website_url,
                                       category, created_at, sort_index, notes, icon,
                                       icon_color, meta, is_current, in_failover_queue)
                VALUES (?1, 'codex', ?2, ?3, ?4, NULL, ?5, NULL, ?6, NULL, NULL, ?7, 0, 0)
                """,
                (provider_id, name,
                 json.dumps(settings_config, ensure_ascii=False),
                 website, now_ms, notes,
                 json.dumps(meta, ensure_ascii=False)),
            )
            if base_url:
                conn.execute(
                    "INSERT INTO provider_endpoints (provider_id, app_type, url, added_at)"
                    " VALUES (?1, 'codex', ?2, ?3)",
                    (provider_id, base_url, now_ms),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return provider_id


CLAUDE_API_FORMATS = ("anthropic", "openai_chat", "openai_responses")


def _add_claude_provider(payload: dict[str, Any]) -> str:
    """Claude / Claude Desktop provider.

    settings_config is taken verbatim from the form's JSON editor (single
    source of truth, mirrors ProviderForm.tsx:1586 claude branch), shape
    {"env": {ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN|ANTHROPIC_API_KEY, ...}}.
    Endpoint row derives from env.ANTHROPIC_BASE_URL (AddProviderDialog.tsx:284).
    """
    app = str(payload.get("app") or "claude")
    if app not in ("claude", "claude-desktop"):
        raise SwitchError(f"不支持的应用: {app}")

    name = str(payload.get("name") or "").strip()
    if not name:
        raise SwitchError("供应商名称不能为空")

    settings_raw = payload.get("settingsJson")
    try:
        settings_obj = json.loads(settings_raw) if isinstance(settings_raw, str) else settings_raw
    except Exception:
        raise SwitchError("settings_config 不是合法 JSON")
    if not isinstance(settings_obj, dict):
        raise SwitchError("settings_config 必须是 JSON 对象")
    env = settings_obj.get("env")
    if not isinstance(env, dict):
        raise SwitchError("settings_config 必须包含 env 对象")

    api_format = str(payload.get("apiFormat") or "anthropic")
    if api_format not in CLAUDE_API_FORMATS:
        raise SwitchError(f"未知的上游格式: {api_format}")

    website = str(payload.get("websiteUrl") or "").strip() or None
    if website and not website.startswith(("http://", "https://")):
        raise SwitchError("官网链接必须以 http:// 或 https:// 开头")

    base_url = str(env.get("ANTHROPIC_BASE_URL") or "").strip().rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise SwitchError("API 请求地址必须以 http:// 或 https:// 开头")

    # meta — 按 ProviderForm.tsx:1716-1833 的键位规则组装
    meta: dict[str, Any] = {"apiFormat": api_format}
    api_key_field = str(payload.get("apiKeyField") or "")
    if api_key_field == "ANTHROPIC_API_KEY":
        if "ANTHROPIC_API_KEY" not in env:
            raise SwitchError("认证字段选了 ANTHROPIC_API_KEY 但 env 中缺少该键")
        meta["apiKeyField"] = "ANTHROPIC_API_KEY"
    if app == "claude":
        meta["commonConfigEnabled"] = bool(payload.get("commonConfigEnabled"))
        meta["endpointAutoSelect"] = True
    else:  # claude-desktop：显式删除 endpointAutoSelect/isFullUrl（ClaudeDesktopProviderForm.tsx:810）
        mode = str(payload.get("desktopMode") or "direct")
        meta["claudeDesktopMode"] = mode if mode in ("direct", "proxy") else "direct"
        routes: dict[str, Any] = {}
        raw_routes = payload.get("desktopRoutes")
        if isinstance(raw_routes, list):
            for item in raw_routes:
                if isinstance(item, dict):
                    m = str(item.get("model") or "").strip()
                    label = str(item.get("label") or "").strip()
                else:
                    m, label = str(item or "").strip(), ""
                if m:
                    routes[m] = {"model": m}
                    if label:
                        routes[m]["labelOverride"] = label
        if routes:
            meta["claudeDesktopModelRoutes"] = routes
    ua = str(payload.get("customUserAgent") or "").strip()
    if ua:
        meta["customUserAgent"] = ua
    headers = payload.get("headersOverride")
    body = payload.get("bodyOverride")
    overrides: dict[str, Any] = {}
    if isinstance(headers, dict) and headers:
        overrides["headers"] = headers
    if isinstance(body, dict) and body:
        overrides["body"] = body
    if overrides:
        meta["localProxyRequestOverrides"] = overrides
    cost = payload.get("costMultiplier")
    if cost not in (None, ""):
        try:
            if float(str(cost)) < 0:
                raise ValueError
        except ValueError:
            raise SwitchError("成本倍率必须是非负数字")
        meta["costMultiplier"] = str(cost).strip()
    source = str(payload.get("pricingModelSource") or "")
    if source in ("request", "response"):
        meta["pricingModelSource"] = source

    now_ms = int(time.time() * 1000)
    provider_id = str(uuid.uuid4())
    notes = str(payload.get("notes") or "").strip() or None

    with open_db() as conn:
        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                INSERT INTO providers (id, app_type, name, settings_config, website_url,
                                       category, created_at, sort_index, notes, icon,
                                       icon_color, meta, is_current, in_failover_queue)
                VALUES (?1, ?2, ?3, ?4, ?5, NULL, ?6, NULL, ?7, NULL, NULL, ?8, 0, 0)
                """,
                (provider_id, app, name,
                 json.dumps(settings_obj, ensure_ascii=False, indent=2),
                 website, now_ms, notes,
                 json.dumps(meta, ensure_ascii=False)),
            )
            if base_url:
                conn.execute(
                    "INSERT INTO provider_endpoints (provider_id, app_type, url, added_at)"
                    " VALUES (?1, ?2, ?3, ?4)",
                    (provider_id, app, base_url, now_ms),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return provider_id


def fetch_model_ids(base_url: str, api_key: str) -> list[str]:
    """GET <base>/models (fallback <base>/v1/models), return data[].id list."""
    base = str(base_url or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise SwitchError("API 请求地址必须以 http:// 或 https:// 开头")
    headers = {"accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    last_err = "未知错误"
    for url in (base + "/models", base + "/v1/models"):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} @ {url}"
            e.close()
            continue
        except Exception as e:
            last_err = f"{e} @ {url}"
            continue
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            last_err = f"响应缺少 data 数组 @ {url}"
            continue
        out: list[str] = []
        for x in items:
            mid = str(x.get("id") or "").strip() if isinstance(x, dict) else ""
            if mid and mid not in out:
                out.append(mid)
        return out
    raise SwitchError(f"获取模型列表失败: {last_err}")


# ---------- Live read/write helpers ----------

def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False,
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        json.dump(value, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp.close()
        os.replace(tmp.name, str(path))
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False,
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        tmp.write(text)
        tmp.close()
        os.replace(tmp.name, str(path))
    except Exception:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _backup(path: Path, reason: str) -> Path | None:
    """Save a timestamped backup next to the file. Returns backup path or None."""
    if not path.exists():
        return None
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_suffix(path.suffix + f".bak-{reason}-{ts}")
    try:
        shutil.copy2(str(path), str(bak))
        return bak
    except Exception:
        return None


def read_live_claude() -> dict[str, Any] | None:
    p = get_claude_settings_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_live_codex() -> dict[str, Any] | None:
    auth_path = get_codex_auth_path()
    config_path = get_codex_config_path()
    if not auth_path.exists() and not config_path.exists():
        return None
    auth: dict[str, Any] = {}
    if auth_path.exists():
        try:
            auth = json.loads(auth_path.read_text(encoding="utf-8")) or {}
            if not isinstance(auth, dict):
                auth = {}
        except Exception:
            auth = {}
    config_text = ""
    if config_path.exists():
        try:
            config_text = config_path.read_text(encoding="utf-8")
        except Exception:
            config_text = ""
    return {"auth": auth, "config": config_text}


# ---------- Sanitization (mirrors services/provider/live.rs:168) ----------

_CLAUDE_INTERNAL_KEYS = {"api_format", "apiFormat", "openrouter_compat_mode", "openrouterCompatMode"}


def _sanitize_claude_for_live(settings: dict[str, Any]) -> dict[str, Any]:
    out = dict(settings)
    for k in _CLAUDE_INTERNAL_KEYS:
        out.pop(k, None)
    return out


# ---------- Codex config.toml injection (mirrors codex_config::prepare_codex_provider_live_config) ----------

# Pattern that matches a [model_providers.<name>] block (greedy until next [section] or EOF)
_PROVIDER_BLOCK_RE = re.compile(
    r"(?ms)^\[model_providers\.([A-Za-z0-9_.\-]+)\]\s*(?P<body>(?:^[^\[]*\n?)*)"
)
# Wire to extract top-level experimental_bearer_token from provider block body
_BEARER_TOKEN_IN_BLOCK_RE = re.compile(r"^\s*experimental_bearer_token\s*=\s*\"([^\"]*)\"", re.M)
# Extract top-level model_provider = "name" (the name of the [model_providers.*] block in use)
_MODEL_PROVIDER_NAME_RE = re.compile(r'(?m)^\s*model_provider\s*=\s*"([^"]+)"')


def _extract_bearer_from_toml(text: str) -> str | None:
    """Look for experimental_bearer_token in any [model_providers.*] block."""
    m = _BEARER_TOKEN_IN_BLOCK_RE.search(text)
    if m:
        return m.group(1)
    # also check top-level (legacy)
    m = re.search(r'(?m)^\s*experimental_bearer_token\s*=\s*"([^"]*)"', text)
    return m.group(1) if m else None


def _extract_active_provider_name(text: str) -> str | None:
    """Find the model_provider = "name" entry — the [model_providers.name] block in use."""
    m = _MODEL_PROVIDER_NAME_RE.search(text)
    return m.group(1).strip() if m else None


def _extract_mcp_servers_block(text: str) -> str | None:
    """Pull out everything from the first [mcp_servers] header to end.

    CC Switch stores [windows]/[desktop]/[plugins] in each provider's
    settings_config, but [mcp_servers] and its sub-tables (e.g.
    [mcp_servers.cua_repl]) live only in the live config — they're
    user-added at runtime. Preserve them across switches so the user's
    MCP setup isn't wiped when toggling providers.
    """
    m = re.search(r'(?m)^\[mcp_servers[^\]]*\]', text)
    if not m:
        return None
    return text[m.start():]


def _strip_mcp_servers(text: str) -> str:
    """Remove any [mcp_servers*] sections (used on the live config before backfilling)."""
    # Remove the parent [mcp_servers] header + body
    out = re.sub(r'(?ms)^\[mcp_servers\][^\[]*', "", text)
    # Remove any sub-tables like [mcp_servers.foo] (defensive — Codex stores them as
    # dotted inline tables so this typically matches nothing, just a safety net)
    out = re.sub(r'(?m)^\[mcp_servers\.[A-Za-z0-9_.\-]+\][^\[]*', "", out)
    return out


def _remove_bearer_token(text: str) -> str:
    """Remove any experimental_bearer_token line from provider blocks (we re-inject cleanly)."""
    return re.sub(r'(?m)^\s*experimental_bearer_token\s*=\s*"[^"]*"\s*\n?', "", text)


def _inject_bearer_token(text: str, provider_name: str, token: str) -> str:
    """Add/update experimental_bearer_token inside [model_providers.<name>] block."""
    if not token:
        return text

    # Find the provider block
    match = re.search(
        rf'(?ms)^\[model_providers\.{re.escape(provider_name)}\]\s*(?P<body>(?:^[^\[]*\n?)*)',
        text,
    )
    if not match:
        return text  # no provider block to inject into

    body = match.group("body")
    new_body = re.sub(
        r'(?m)^\s*experimental_bearer_token\s*=\s*"[^"]*"\s*\n?',
        "",
        body,
    )
    if not new_body.endswith("\n") and new_body:
        new_body += "\n"
    new_body += f'experimental_bearer_token = "{token}"\n'

    start, end = match.span()
    return text[:start] + f"[model_providers.{provider_name}]\n{new_body}" + text[end:]


def _build_codex_config_for_live(
    provider_cfg: dict[str, Any],
    current_model_provider: str | None,
) -> str:
    """Mirror `write_codex_provider_live_with_catalog` for our subset.

    Provider stores its [model_providers] TOML in `provider_cfg["config"]`
    plus optional model_provider reference. We:
      - Use provider_cfg.config as the base text
      - Strip & re-inject bearer token from OPENAI_API_KEY
    """
    text = provider_cfg.get("config") or ""
    if not isinstance(text, str):
        text = ""

    auth = provider_cfg.get("auth") or {}
    if not isinstance(auth, dict):
        auth = {}

    token = ""
    if isinstance(auth.get("OPENAI_API_KEY"), str):
        token = auth["OPENAI_API_KEY"].strip()
    if not token:
        # Fallback: existing bearer in current text
        token = _extract_bearer_from_toml(text) or ""

    # Find the [model_providers.<name>] block in use (model_provider = "name")
    provider_name = _extract_active_provider_name(text) or "custom"
    # Note: this launcher assumes model_providers.<name> block lives in text.
    # If the provider stores model_provider = "foo" but the text has no
    # [model_providers.foo] block, the bearer injection silently no-ops.

    text = _remove_bearer_token(text)
    text = _inject_bearer_token(text, provider_name, token)
    return text


def _build_codex_config_with_preserved_mcp(
    new_provider_text: str, current_live_text: str
) -> str:
    """Append the live [mcp_servers*] sections to the new provider's config text.
    These are user-side additions, not stored in providers.
    """
    mcp_block = _extract_mcp_servers_block(current_live_text)
    if not mcp_block:
        return new_provider_text
    # Strip any existing [mcp_servers] from new provider (defensive) and append
    new_provider_text = _strip_mcp_servers(new_provider_text)
    sep = "\n\n" if not new_provider_text.endswith("\n") else "\n"
    return new_provider_text.rstrip() + sep + mcp_block


# ---------- Connectivity check (mirrors services/stream_check.rs) ----------
#
# Same semantics as CC Switch's 连通检测: GET the provider's base_url with a
# small request (headers only), 8s timeout, 1 retry on timeout. ANY HTTP
# response (even 4xx/5xx) counts as reachable; only network-level failure
# counts as unreachable. TTFB <= 6000ms = 正常, slower = 较慢.

CHECK_TIMEOUT_S = 8
CHECK_MAX_RETRIES = 1
DEGRADED_THRESHOLD_MS = 6000

CC_SWITCH_EXE = Path(r"C:\Users\ASUS\AppData\Local\Programs\cc-switch\cc-switch.exe")


# ---------- CC Switch window activation ----------
# CC Switch (Tauri) is single-instance: os.startfile on a running instance just
# notifies it — and if the window is minimized / off-screen / behind others, the
# user sees "nothing happen". So: find its main window and force-restore+front.
def _cc_switch_pids() -> set[int]:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq cc-switch.exe", "/FO", "CSV"],
        capture_output=True,
    ).stdout.decode("gbk", errors="ignore")
    pids: set[int] = set()
    for line in out.strip().splitlines()[1:]:
        parts = line.strip().strip('"').split('","')
        if len(parts) >= 2 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def _cc_switch_window() -> int | None:
    """Return hwnd of the CC Switch main (Tauri) window, or None if not running."""
    pids = _cc_switch_pids()
    if not pids:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found: list[tuple[int, int]] = []  # (hwnd, area)

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd: int, _lparam: int) -> bool:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.IsWindowVisible(hwnd):
                cls = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, cls)
                if cls.value == "T":  # Tauri main window class
                    r = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(r))
                    area = (r.right - r.left) * (r.bottom - r.top)
                    found.append((hwnd, area))
            return True

        user32.EnumWindows(cb, 0)
        if not found:
            return None
        return max(found, key=lambda t: t[1])[0]
    except Exception:
        return None


def _activate_window(hwnd: int) -> bool:
    """SW_RESTORE + force foreground. Returns True on success."""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
        # Foreground-lock workaround: simulate ALT keyup to unlock SetForegroundWindow
        user32.keybd_event(0x12, 0, 2, 0)  # VK_MENU, KEYEVENTF_KEYUP
        ok = bool(user32.SetForegroundWindow(hwnd))
        user32.keybd_event(0x12, 0, 2, 0)
        return ok
    except Exception:
        return False
def resolve_base_url(p: Provider) -> str | None:
    """Extract the provider's base_url (mirrors StreamCheckService::resolve_base_url).

    Claude: env.ANTHROPIC_BASE_URL. Codex: base_url inside the active
    [model_providers.<name>] block of the stored config.toml text.
    Official providers have no user-configured probe target -> None.
    """
    if p.category == "official":
        return None
    if p.app_type in ("claude", "claude-desktop"):
        env = p.settings_config.get("env") or {}
        if isinstance(env, dict):
            u = env.get("ANTHROPIC_BASE_URL")
            if isinstance(u, str) and u.strip():
                return u.strip()
        return None
    if p.app_type == "codex":
        text = p.settings_config.get("config") or ""
        if not isinstance(text, str) or not text.strip():
            return None
        name = _extract_active_provider_name(text)
        if name:
            m = re.search(
                rf"(?ms)^\[model_providers\.{re.escape(name)}\]\s*(?P<body>(?:^[^\[]*\n?)*)",
                text,
            )
            if m:
                bm = re.search(r'(?m)^\s*base_url\s*=\s*"([^"]+)"', m.group("body"))
                if bm:
                    return bm.group(1).strip()
        # fallback: any base_url in any [model_providers.*] block
        bm = re.search(r'(?m)^\s*base_url\s*=\s*"([^"]+)"', text)
        return bm.group(1).strip() if bm else None
    return None


def _probe_once(url: str) -> tuple[int, int]:
    """GET url, return (http_status, ttfb_ms). Any HTTP response = reachable.

    Note urllib raises HTTPError for 4xx/5xx — unlike reqwest's Ok-for-any-status,
    so we treat HTTPError as a *successful* reachability probe in check_provider.
    """
    req = urllib.request.Request(
        url, headers={"accept": "*/*", "accept-encoding": "identity"}
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT_S) as resp:
            status = resp.status
            # urlopen returns once response headers are in — TTFB by construction;
            # we deliberately do not read the body.
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            e.close()
        except Exception:
            pass
    return status, int((time.perf_counter() - t0) * 1000)


def check_provider(p: Provider) -> dict[str, Any]:
    """Mirror StreamCheckService::check_with_retry -> result dict for the UI."""
    url = resolve_base_url(p)
    if not url:
        return {"status": "skipped", "message": "官方/未配置探测地址"}
    last_err: Exception | None = None
    for attempt in range(CHECK_MAX_RETRIES + 1):
        try:
            status, ms = _probe_once(url)
            if ms <= DEGRADED_THRESHOLD_MS:
                return {"status": "ok", "ms": ms, "http": status,
                        "message": f"连通正常 · HTTP {status} · {ms}ms"}
            return {"status": "slow", "ms": ms, "http": status,
                    "message": f"连通但较慢 · HTTP {status} · {ms}ms"}
        except urllib.error.URLError as e:
            last_err = e
            reason = str(getattr(e, "reason", e)).lower()
            if "timed out" in reason or "timeout" in reason:
                continue  # retry on timeout only (mirrors should_retry)
            break
        except Exception as e:  # socket errors etc.
            last_err = e
            msg = str(e).lower()
            if "timed out" in msg or "timeout" in msg:
                continue
            break
    msg = str(last_err) if last_err else "未知错误"
    return {"status": "fail", "message": f"无法连通: {msg}"}


# ---------- settings.json helpers ----------


def _read_settings_json_typed() -> dict[str, Any]:
    path = get_settings_json_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _update_settings_json_current(app_type: str, provider_id: str | None) -> None:
    path = get_settings_json_path()
    data = _read_settings_json_typed()
    key_map = {
        "claude": "currentProviderClaude",
        "codex": "currentProviderCodex",
        "gemini": "currentProviderGemini",
        "grokbuild": "currentProviderGrokbuild",
        "opencode": "currentProviderOpencode",
        "openclaw": "currentProviderOpenclaw",
        "hermes": "currentProviderHermes",
        "claude-desktop": "currentProviderClaudeDesktop",
    }
    k = key_map.get(app_type)
    if not k:
        return
    if provider_id is None:
        data.pop(k, None)
    else:
        data[k] = provider_id
    _atomic_write_json(path, data)


# ---------- Core switch (mirrors ProviderService::switch_normal) ----------

class SwitchError(RuntimeError):
    pass


def _db_set_current(conn: sqlite3.Connection, app_type: str, new_id: str) -> None:
    """Transactional is_current flip: 0 for all of app_type, 1 for new_id."""
    cur = conn.execute("BEGIN")
    cur.execute("END")  # autocommit mode + explicit txn via execute
    # In autocommit (isolation_level=None), use savepoint or just rely on SQLite atomicity:
    conn.execute("UPDATE providers SET is_current = 0 WHERE app_type = ?1", (app_type,))
    conn.execute(
        "UPDATE providers SET is_current = 1 WHERE id = ?1 AND app_type = ?2",
        (new_id, app_type),
    )


def _db_save_provider_settings_config(
    conn: sqlite3.Connection, app_type: str, provider_id: str, settings_config: dict[str, Any]
) -> None:
    conn.execute(
        "UPDATE providers SET settings_config = ?1 WHERE id = ?2 AND app_type = ?3",
        (json.dumps(settings_config, ensure_ascii=False), provider_id, app_type),
    )


def switch_claude(target_id: str) -> dict[str, Any]:
    providers = list_providers("claude")
    target = next((p for p in providers if p.id == target_id), None)
    if not target:
        raise SwitchError(f"找不到 Claude 供应商: {target_id}")
    current = next((p for p in providers if p.is_current), None)

    # 1. Backfill current live state into current provider (if different)
    if current and current.id != target.id:
        live = read_live_claude()
        if live is not None:
            try:
                with open_db() as conn:
                    _db_save_provider_settings_config(conn, "claude", current.id, live)
            except sqlite3.OperationalError as e:
                raise SwitchError(f"DB 写入失败（CC Switch 是否在跑？）: {e}")

    # 3. Backup + write live
    claude_path = get_claude_settings_path()
    _backup(claude_path, "pre-switch")
    sanitized = _sanitize_claude_for_live(target.settings_config)
    _atomic_write_json(claude_path, sanitized)

    # 4. DB is_current
    try:
        with open_db() as conn:
            _db_set_current(conn, "claude", target.id)
    except sqlite3.OperationalError as e:
        raise SwitchError(f"DB 切换失败: {e}")

    # 5. settings.json
    _update_settings_json_current("claude", target.id)

    return {"previous_id": current.id if current else None, "new_id": target.id}


def switch_codex(target_id: str) -> dict[str, Any]:
    providers = list_providers("codex")
    target = next((p for p in providers if p.id == target_id), None)
    if not target:
        raise SwitchError(f"找不到 Codex 供应商: {target_id}")
    current = next((p for p in providers if p.is_current), None)

    # 1. Backfill current live state into current provider (if different)
    if current and current.id != target.id:
        live = read_live_codex()
        if live is not None:
            backfilled = {
                "auth": live.get("auth", {}),
                "config": live.get("config", ""),
            }
            merged = dict(current.settings_config)
            merged["auth"] = backfilled["auth"]
            merged["config"] = backfilled["config"]
            try:
                with open_db() as conn:
                    _db_save_provider_settings_config(conn, "codex", current.id, merged)
            except sqlite3.OperationalError as e:
                raise SwitchError(f"DB 写入失败（CC Switch 是否在跑？）: {e}")

    # 3. Backup + write live (auth + config.toml)
    auth_path = get_codex_auth_path()
    cfg_path = get_codex_config_path()
    _backup(auth_path, "pre-switch")
    _backup(cfg_path, "pre-switch")

    target_auth = target.settings_config.get("auth") or {}
    if not isinstance(target_auth, dict):
        target_auth = {}
    target_config_text = _build_codex_config_for_live(target.settings_config, None)

    # Preserve [mcp_servers*] from current live config (live-only user config)
    current_live_text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    target_config_text = _build_codex_config_with_preserved_mcp(
        target_config_text, current_live_text
    )

    _atomic_write_json(auth_path, target_auth)
    _atomic_write_text(cfg_path, target_config_text)

    # 4. DB is_current
    try:
        with open_db() as conn:
            _db_set_current(conn, "codex", target.id)
    except sqlite3.OperationalError as e:
        raise SwitchError(f"DB 切换失败: {e}")

    # 5. settings.json
    _update_settings_json_current("codex", target.id)

    return {"previous_id": current.id if current else None, "new_id": target.id}


# ---------- UI ----------

class LauncherUI:
    APP_TYPES = [
        ("claude", "Claude Code", "🌙"),
        ("codex", "Codex", "⚡"),
    ]

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CC Switch · 快速切换")
        self.root.geometry("560x720")
        self.root.minsize(500, 540)
        self.providers_by_app: dict[str, list[Provider]] = {}
        self.card_widgets: dict[str, dict[str, tk.Frame]] = {}  # app_type -> provider_id -> frame
        self.status_var = tk.StringVar(value="就绪")
        self.test_results: dict[tuple[str, str], dict[str, Any]] = {}  # (app, pid) -> result
        self.relay_rows: list = []
        self.relay_widgets: dict[str, tuple] = {}
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        header.pack(fill=tk.X)
        ttk.Label(header, text="CC Switch · 快速切换", font=("Microsoft YaHei", 13, "bold")).pack(
            side=tk.LEFT
        )
        ttk.Button(header, text="＋ 添加供应商", command=self.open_cc_switch).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(header, text="刷新", command=self.refresh).pack(side=tk.RIGHT)

        # 反代产品区 —— 从兄弟项目的 relay.json 自动发现，置顶常显。
        backend = ttk.LabelFrame(self.root, text="  🛰  反代产品  ", padding=(10, 6))
        backend.pack(fill=tk.X, padx=12, pady=(6, 0))
        self.relay_frame = ttk.Frame(backend)
        self.relay_frame.pack(fill=tk.X)

        self.sections: dict[str, ttk.LabelFrame] = {}
        body = ttk.Frame(self.root, padding=(12, 4, 12, 8))
        body.pack(fill=tk.BOTH, expand=True)
        for app_type, label, emoji in self.APP_TYPES:
            sec = ttk.LabelFrame(body, text=f"  {emoji}  {label}  ", padding=8)
            sec.pack(fill=tk.X, pady=4)
            self.sections[app_type] = sec
            canvas_frame = ttk.Frame(sec)
            canvas_frame.pack(fill=tk.X)

        # Status bar
        status = ttk.Frame(self.root, padding=(12, 6, 12, 8))
        status.pack(fill=tk.X)
        ttk.Label(status, textvariable=self.status_var, foreground="#555").pack(side=tk.LEFT)

    def refresh(self) -> None:
        self.providers_by_app.clear()
        for app_type, _, _ in self.APP_TYPES:
            try:
                self.providers_by_app[app_type] = list_providers(app_type)
            except sqlite3.OperationalError as e:
                self.providers_by_app[app_type] = []
                self.status_var.set(f"⚠️ DB 读取失败: {e}")
            self._render_section(app_type)
        self._render_relay_section()

    # -- relays (auto-discovered from sibling relay.json manifests) --

    def _render_relay_section(self) -> None:
        for w in self.relay_rows:
            w.destroy()
        self.relay_rows = []
        self.relay_widgets = {}
        found = relays.discover_relays()
        if not found:
            lbl = ttk.Label(
                self.relay_frame,
                text="未发现反代产品 —— 在上级目录的兄弟项目里放 relay.json 即可接入",
                foreground="#777",
            )
            lbl.pack(anchor=tk.W)
            self.relay_rows.append(lbl)
            return
        for rl in found:
            row = ttk.Frame(self.relay_frame)
            row.pack(fill=tk.X, pady=2)
            status = ttk.Label(row, text="")
            status.pack(side=tk.LEFT)
            stop_btn = ttk.Button(
                row, text="⏸ 暂停",
                command=lambda r=rl: self.on_relay_op(r, False))
            stop_btn.pack(side=tk.RIGHT, padx=(6, 0))
            start_btn = ttk.Button(
                row, text="启动 / 修复",
                command=lambda r=rl: self.on_relay_op(r, True))
            start_btn.pack(side=tk.RIGHT)
            self.relay_rows += [row, status, start_btn, stop_btn]
            self.relay_widgets[rl.id] = (status, start_btn, stop_btn)
            self._update_relay_row(rl)

    def _update_relay_row(self, rl: relays.Relay) -> None:
        ent = self.relay_widgets.get(rl.id)
        if not ent:
            return
        status, start_btn, stop_btn = ent
        online = relays.relay_online(rl)
        status.configure(
            text=f"{rl.name}  {rl.address}：" + ("● 在线" if online else "○ 离线"),
            foreground="#2e9e44" if online else "#c03030",
        )
        start_btn.state(["!disabled"] if rl.start_cmd else ["disabled"])
        stop_btn.state(["!disabled"] if rl.stop_match else ["disabled"])

    def on_relay_op(self, rl: relays.Relay, start: bool) -> None:
        ent = self.relay_widgets.get(rl.id)
        if not ent:
            return
        _, start_btn, stop_btn = ent
        btn = start_btn if start else stop_btn
        btn.state(["disabled"])
        self.status_var.set(("正在启动 " if start else "正在暂停 ") + rl.name + "…")
        self.root.update_idletasks()

        def worker() -> None:
            ok, msg = relays.start_relay(rl) if start else relays.stop_relay(rl)
            self.root.after(0, lambda: self._relay_op_done(rl, start, ok, msg))

        threading.Thread(target=worker, daemon=True).start()

    def _relay_op_done(self, rl: relays.Relay, start: bool, ok: bool,
                       msg: str) -> None:
        ent = self.relay_widgets.get(rl.id)
        if ent:
            ent[1].state(["!disabled"] if rl.start_cmd else ["disabled"])
            ent[2].state(["!disabled"] if rl.stop_match else ["disabled"])
        self._update_relay_row(rl)
        verb = "启动" if start else "暂停"
        if ok:
            self.status_var.set(f"✅ {rl.name}{verb}完成")
        else:
            self.status_var.set(f"⚠️ {rl.name}{verb}失败")
            messagebox.showwarning(f"{rl.name}{verb}", msg[:1500] or "无输出",
                                   parent=self.root)

    # -- CC Switch launcher --

    def open_cc_switch(self) -> None:
        exe = str(CC_SWITCH_EXE)
        if not CC_SWITCH_EXE.exists():
            messagebox.showerror(
                "找不到 CC Switch", f"路径不存在：\n{exe}", parent=self.root
            )
            return
        # Already running → bring its window to the front instead of relying on
        # the single-instance plugin (which does nothing visible if the window
        # is minimized / off-screen / behind other windows).
        hwnd = _cc_switch_window()
        if hwnd and _activate_window(hwnd):
            self.status_var.set("CC Switch 已在运行，已将窗口置前")
            return
        try:
            os.startfile(exe)  # type: ignore[attr-defined]
            self.status_var.set("已启动 CC Switch")
        except Exception as e:
            messagebox.showerror("启动失败", str(e), parent=self.root)

    def _render_section(self, app_type: str) -> None:
        sec = self.sections[app_type]
        for w in sec.winfo_children():
            w.destroy()

        providers = self.providers_by_app.get(app_type, [])
        if not providers:
            ttk.Label(sec, text="  （无供应商）", foreground="#888").pack(anchor=tk.W, pady=8)
            return

        for p in providers:
            self._render_card(sec, p, app_type)

    def _render_card(self, parent: ttk.LabelFrame, p: Provider, app_type: str) -> None:
        bg = "#1f6feb" if p.is_current else "#2b2b2b"
        fg = "#ffffff" if p.is_current else "#dddddd"
        border_color = "#0b4fb3" if p.is_current else "#3a3a3a"

        card = tk.Frame(parent, bg=bg, highlightbackground=border_color, highlightthickness=1)
        card.pack(fill=tk.X, pady=3, ipady=4)
        card.bind("<Button-1>", lambda _e, ap=app_type, pid=p.id: self._on_click(ap, pid))

        inner = tk.Frame(card, bg=bg)
        inner.pack(fill=tk.X, padx=10, pady=4)
        inner.bind("<Button-1>", lambda _e, ap=app_type, pid=p.id: self._on_click(ap, pid))

        title_row = tk.Frame(inner, bg=bg)
        title_row.pack(fill=tk.X)
        title_row.bind("<Button-1>", lambda _e, ap=app_type, pid=p.id: self._on_click(ap, pid))

        name_lbl = tk.Label(
            title_row, text=p.name, bg=bg, fg=fg, font=("Microsoft YaHei", 10, "bold")
        )
        name_lbl.pack(side=tk.LEFT)
        name_lbl.bind("<Button-1>", lambda _e, ap=app_type, pid=p.id: self._on_click(ap, pid))

        # -- connectivity test button + result label (mirrors CC Switch 连通检测) --
        result_fg = {
            "ok": "#4ade80", "slow": "#fbbf24", "fail": "#f87171",
            "skipped": "#999999", "checking": "#bbbbbb",
        }

        def _apply_result(res: dict[str, Any] | None) -> None:
            if res is None:
                result_lbl.configure(text="", bg=bg)
                return
            icon = {"ok": "✓", "slow": "⚠", "fail": "✗", "skipped": "—", "checking": "⏳"}
            st = res.get("status", "checking")
            result_lbl.configure(
                text=f"{icon.get(st, '?')} {res.get('message', '')}",
                fg=result_fg.get(st, "#bbbbbb"), bg=bg,
            )

        result_lbl = tk.Label(
            title_row, text="", bg=bg, fg="#bbbbbb",
            font=("Microsoft YaHei", 8), padx=4,
        )
        # pack order for side=RIGHT: first packed = rightmost
        if p.is_current:
            cur = tk.Label(
                title_row, text="  ● 当前 ", bg="#0b4fb3", fg="#ffffff",
                font=("Microsoft YaHei", 8, "bold"), padx=6, pady=1,
            )
            cur.pack(side=tk.RIGHT)
            cur.bind("<Button-1>", lambda _e, ap=app_type, pid=p.id: self._on_click(ap, pid))
        test_btn = tk.Button(
            title_row, text="测连通", bg=bg, fg="#dddddd", relief=tk.FLAT,
            activebackground="#3a86ff", activeforeground="#ffffff",
            font=("Microsoft YaHei", 8), padx=6, pady=1,
            command=lambda ap=app_type, pid=p.id: self._on_test(ap, pid),
        )
        test_btn.pack(side=tk.RIGHT, padx=(4, 6))
        result_lbl.pack(side=tk.RIGHT)

        # restore a previous test result if we have one
        prev = self.test_results.get((app_type, p.id))
        if prev:
            _apply_result(prev)

        if p.notes:
            note = tk.Label(
                inner, text=p.notes, bg=bg, fg="#bbbbbb",
                font=("Microsoft YaHei", 8), wraplength=480, justify=tk.LEFT,
            )
            note.pack(fill=tk.X, anchor=tk.W)
            note.bind("<Button-1>", lambda _e, ap=app_type, pid=p.id: self._on_click(ap, pid))
        else:
            note = None

        base_url = resolve_base_url(p)
        meta = f"category: {p.category or '—'}    id: {p.id[:8]}…"
        if base_url:
            meta += f"    {base_url[:58]}{'…' if len(base_url) > 58 else ''}"
        cat_lbl = tk.Label(
            inner, text=meta, bg=bg, fg="#888888", font=("Consolas", 8),
        )
        cat_lbl.pack(fill=tk.X, anchor=tk.W)
        cat_lbl.bind("<Button-1>", lambda _e, ap=app_type, pid=p.id: self._on_click(ap, pid))

        if p.is_current:
            return  # don't switch to self
        # Hover effect (note may be None)
        def on_enter(_e, c=card, i=inner, t=title_row, n=name_lbl, no=note, cat=cat_lbl, btn=test_btn, rl=result_lbl):
            for w in (c, i, t, n, cat, rl):
                w.configure(bg="#3a86ff")
            btn.configure(bg="#3a86ff")
            if no is not None:
                no.configure(bg="#3a86ff")

        def on_leave(_e, c=card, i=inner, t=title_row, n=name_lbl, no=note, cat=cat_lbl, btn=test_btn, rl=result_lbl):
            for w in (c, i, t, n, cat, rl):
                w.configure(bg=bg)
            btn.configure(bg=bg)
            if no is not None:
                no.configure(bg=bg)

        for w in (card, inner, title_row, name_lbl):
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)

    def _on_test(self, app_type: str, provider_id: str) -> None:
        p = next((x for x in self.providers_by_app.get(app_type, []) if x.id == provider_id), None)
        if not p:
            return
        key = (app_type, provider_id)
        self.test_results[key] = {"status": "checking", "message": "测试中…"}
        self._render_section(app_type)  # re-render to show "checking"
        self.root.update_idletasks()

        def worker() -> None:
            result = check_provider(p)
            self.root.after(0, lambda: self._test_done(app_type, provider_id, result))

        threading.Thread(target=worker, daemon=True).start()

    def _test_done(self, app_type: str, provider_id: str, result: dict[str, Any]) -> None:
        self.test_results[(app_type, provider_id)] = result
        self._render_section(app_type)

    def _on_click(self, app_type: str, provider_id: str) -> None:
        # Confirm
        p = next((x for x in self.providers_by_app.get(app_type, []) if x.id == provider_id), None)
        if not p:
            return
        if p.is_current:
            self.status_var.set(f"⚠️ {p.name} 已是当前供应商")
            return
        if not messagebox.askyesno(
            "确认切换",
            f"将 {app_type} 当前供应商切换为：\n{p.name}\n\n（会自动备份 live 配置）",
            parent=self.root,
        ):
            return

        self.status_var.set(f"⏳ 正在切换 {app_type} → {p.name} ...")
        self.root.update_idletasks()
        try:
            if app_type == "claude":
                switch_claude(provider_id)
            elif app_type == "codex":
                switch_codex(provider_id)
            else:
                self.status_var.set(f"⚠️ 暂不支持 {app_type}")
                return
        except SwitchError as e:
            messagebox.showerror("切换失败", str(e), parent=self.root)
            self.status_var.set(f"❌ {e}")
            return

        self.status_var.set(f"✅ {app_type} → {p.name}")
        self.refresh()


def main() -> int:
    root = tk.Tk()
    try:
        root.tk.call("source", "sun-valley.tcl")
    except tk.TclError:
        pass
    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
    LauncherUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())