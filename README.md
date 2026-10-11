# proxy_switch

本仓库是三个本地代理 / 切换工具项目的快照，按**分支**存放，一个分支对应一个项目：

| 分支 | 项目 | 作用 | 默认端口 |
|---|---|---|---|
| `controller` | Controller（控制器） | 一键切换 Claude Code / Codex 供应商的轻量桌面面板，附带自动发现的反代产品管理 | `127.0.0.1:18777` |
| `autoclaw-switch` | A-SWITCH | AutoClaw 多账号管理 + 本地反代，把 AutoClaw 账号积分变成 ZCode 可直接选用的模型 | `127.0.0.1:18766` |
| `workbuddy2api-hub` | WorkBuddy2API-Hub | 把 WorkBuddy（国际版 workbuddy.ai / 国内版 codebuddy.cn）封装成 OpenAI 兼容 + Anthropic Messages 接口的多账号网关 | `127.0.0.1:8788` |

## 查看某个项目

```bash
git clone https://github.com/chasel12/proxy_switch.git
cd proxy_switch

git switch controller          # 或 autoclaw-switch / workbuddy2api-hub
```

各分支互相独立，切换后目录内容即为该项目的完整源码。

## 三个项目的关系

三者平级，互不依赖，但可以配合使用：

- **controller** 不认识任何具体反代产品，只读取兄弟目录根部的 `relay.json` 清单，
  在面板的「反代产品状态条」里做健康检查 / 启动 / 暂停；
- **autoclaw-switch** 与 **workbuddy2api-hub** 各自提供 `relay.json`，
  被 controller 自动发现后即可在面板里一键启停；
- 供应商切换走 [CC Switch](https://github.com/farion1231/cc-switch) 的数据库（`~/.cc-switch/cc-switch.db`），
  controller 直接读写该库完成 Claude Code / Codex 的供应商切换，无需打开 CC Switch 的 Tauri GUI。

典型链路：

```
Codex / Claude Code 客户端
        │  （CC Switch 切到对应 provider）
        ▼
controller (18777) ── 启停管理 ──▶ autoclaw-switch relay (18766)
                     └─ 启停管理 ─▶ workbuddy2api-hub (8788)
```

## 各项目简介

### `controller` 分支 — Controller（控制器）

- 单文件 `控制器.exe`（PyInstaller 打包，无控制台、带图标，双击即用）+ 纯 Python 标准库源码；
- 界面复刻 CC Switch 官方深色玻璃设计，Edge `--app` 模式开成无浏览器边框的桌面窗口；
- 切换动作 = 回填 live 配置 → 写 `~/.claude/settings.json` / `~/.codex/auth.json` + `config.toml` → 更新 DB `is_current` → 更新 `~/.cc-switch/settings.json`；
- 不依赖 Electron / Tauri / pip。

### `autoclaw-switch` 分支 — A-SWITCH

- **多账号**：一个窗口里加号、切号、看余额、批量签到领积分；
- **防封**：暖号、限速（单号 12 次/分）、并发闸、日预算，参数按真实封号流水标定；
- **反代**：把 AutoClaw 账号注册成 ZCode 的模型供应商，消耗 AutoClaw 积分；
- **2.x 适配**：AutoClaw 2.0.1 官方渠道的凭证桥接、网关新契约、Electron 管理控制台，端到端实测通过；
- 国内 / 国际双线统一在同一个端口（18766）按号选线。

### `workbuddy2api-hub` 分支 — WorkBuddy2API-Hub

- OpenAI 兼容（Chat Completions / Responses）+ 原生 Anthropic Messages 三协议；
- 双区域（国际版 / 国内版）独立路由，Web 看板一键切换，状态落盘；
- 多账号调度、设备指纹隔离、每日自动任务、四条账号级限额护栏、按 Key 出口绑定；
- 绿色包自带精简 Python，双击 `start-wb-proxy.bat` 即启。

## 说明

- 各分支为**源码快照**（单次提交），不含各项目原有的上游 git 历史；
- 账号凭据、用量统计、运行时日志等敏感数据已被各项目的 `.gitignore` 排除，不会进入本仓库；
- 三个项目各自独立，license 以各分支内文件为准（workbuddy2api-hub 为 MIT）。
