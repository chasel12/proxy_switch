# Controller（控制器）

一键切换 Claude Code / Codex 供应商的轻量桌面面板，附带**自动发现的反代产品管理**。
**不依赖 Electron / Tauri / pip 包**，Python 标准库本地服务 + 纯 HTML/CSS/JS 界面，
用 Edge `--app` 模式开成无浏览器边框的桌面窗口。UI 复刻 CC Switch 官方设计语言
（深色玻璃卡片 / 蓝色发光当前态 / 悬停微动效）。

> 适用场景：你已经用 [CC Switch](https://github.com/farion1231/cc-switch) 配好多个供应商，
> 但不想每次开 Tauri GUI 来回切。用这个直接点一下就切。

与反代项目（如 `../autoclaw-switch/`）**平级解耦**：控制台不认识任何具体反代产品，
只读各兄弟项目根目录的 `relay.json` 清单（见下文「反代产品状态条」）。

## 文件

| 文件 | 作用 |
|---|---|
| `控制器.exe` | 打包后的应用本体（单文件、无控制台、带图标，双击即用无需 Python） |
| `webui.py` | 本地 HTTP 服务（127.0.0.1:18777）+ API + Edge 应用窗口启动（exe 入口） |
| `controller.html` | 界面（CC Switch 深色设计语言，纯前端，无外部资源） |
| `cc_switch_launcher.py` | 逻辑层（DB / 切换 / 连通检测），被 webui.py 导入；`--tk` 可回退旧 tkinter 界面 |
| `relays.py` | 反代产品通用引擎：发现兄弟目录的 `relay.json` 清单，做健康检查 / 启动 / 暂停 |
| `CONTROLLER.spec` + `assets/controller.ico` | PyInstaller 打包配置与图标 |

## 它做什么

读 `~/.cc-switch/cc-switch.db`（CC Switch v2/v3 兼容），按 app 分组展示供应商卡片。
点击卡片会原子地完成 CC Switch 内部 `ProviderService::switch_normal` 的核心动作：

1. **Backfill**：切走前把当前 live 配置回填到「当前供应商」的 `settings_config`
   （保护你在 live 文件里手动改的东西）
2. **写 live**：
   - Claude：`~/.claude/settings.json`（自动 sanitize 内部字段）
   - Codex：`~/.codex/auth.json` + `~/.codex/config.toml`（自动注入 bearer token，
     保留 `[mcp_servers*]` 用户侧配置）
3. **DB 更新**：`is_current` 字段翻转（事务）
4. **更新** `~/.cc-switch/settings.json` 的 `currentProviderClaude` / `currentProviderCodex`

## 使用

1. 双击 `控制器.exe` → 无控制台窗口 + Edge 应用窗口打开面板（重复点击只会拉起窗口，
   不会开第二个服务）；面板右上角「⏻ 退出」按钮才会结束后台服务。
   开发调试直接 `python webui.py`
2. 当前供应商：蓝色描边 + 左侧蓝色渐变光晕 + 「● 当前」徽章
3. 点击其他供应商卡片 → 弹出确认框 → 确认后秒切
4. 切换前自动 `.bak-pre-switch-YYYYMMDD-HHMMSS` 备份 live 文件
5. 旧版 tkinter 界面保留：`python webui.py --tk`

### 打包 exe

```
pip install pyinstaller
pyinstaller CONTROLLER.spec --noconfirm    # 产物 dist/控制器.exe
```

运行时仍是 **0 第三方依赖**（PyInstaller 只在打包时用）。`controller.html` 会被打进
exe（`datas`），frozen 下从 `sys._MEIPASS` 读取。

### 界面设计（对齐 CC Switch 官方前端）

- 设计 token 取自 `cc-switch-3.20.4/src/index.css` 的 dark 主题（背景 #17171b、卡片玻璃渐变、
  边框 #31313a、主色 #3b82f6、12px 圆角）
- 卡片：hover 上浮 2px + 边框提亮；当前项 `border-blue-500/60` 发光描边 + 左→右蓝色渐变洗底
- 徽章体系：分类（官方/国内官方/自定义/聚合）+「● 当前」蓝色发光徽章
- 品牌色头像瓦片（取 DB `icon_color`，首字母/DS/OA 字形）
- 自定义确认弹窗（毛玻璃遮罩、卡片缩放入场）、sonner 风格右下角 Toast、底部状态栏
- 连通检测结果显示在卡片右侧（绿/黄/红圆点 + HTTP + TTFB），测试中带脉冲动画

### 连通性测试（每张卡片的「测连通」按钮）

与 CC Switch 内置「连通检测」完全同款逻辑（`services/stream_check.rs`）：

- 从供应商配置提取 base_url（Claude 取 `env.ANTHROPIC_BASE_URL`；Codex 取活跃
  `[model_providers.X]` 块的 `base_url`），**只发 GET 探测请求头，不发真实模型请求**
- 8 秒超时、超时自动重试 1 次；TTFB ≤ 6000ms 显示绿色 ✓，超过显示黄色 ⚠，连不上显示红色 ✗
- **任何 HTTP 响应（包括 401/404/500）都算「可达」**——只验证地址通不通，不代表鉴权/模型配置正确
- 官方 OAuth 供应商（category=official）无用户配置的探测地址，显示灰色 — 跳过
- 测试结果保留在会话内，切换/刷新后仍显示

### ＋ 添加供应商（面板内置表单）

打开面板内「添加供应商」页面，手动填写全部字段后直接写入共享 DB：

- **基础信息**：供应商名称*、备注、官网链接、默认模型、API Key*（自动填充下方 auth.json）、API 请求地址（写入 config.toml 并登记测速端点）
- **高级选项**：上游格式（Responses / Chat / Anthropic Messages）、模型映射（+手动添加 / 从端点获取模型列表，生成 model_catalog_json）、自定义 User-Agent、本地代理请求覆盖（Header / Body 覆盖，JSON）
- **auth.json / config.toml** 可视化编辑器：auth 随 API Key 自动填充，config 按字段实时生成模板（支持「重置模板」「格式化」）
- **计费配置**：可勾选「使用单独配置」设置成本倍率与计价来源

写库结构与 CC Switch `ProviderService::add` 完全一致（`providers` + `provider_endpoints`，
`category/sort_index` 为空、`is_current=0`），CC Switch 中即时可见；页面右上保留
「在 CC Switch 中打开」入口。

**三个页签区分客户端与终端**（与 CC Switch 的 app 对应）：

| 页签 | app_type | settings_config | meta 要点 |
|---|---|---|---|
| ⚡ Codex（终端） | `codex` | `{auth, config, modelCatalog?}` | `apiFormat` + UA/覆盖/计费 |
| 🌙 Claude Code（终端） | `claude` | `{env}`（AUTH_TOKEN/API_KEY、BASE_URL、模型角色映射） | `apiFormat`、`commonConfigEnabled`、`endpointAutoSelect`、UA/覆盖/计费 |
| 🖥 Claude Desktop（客户端） | `claude-desktop` | `{env}`（仅 BASE_URL + key） | `claudeDesktopMode`、`claudeDesktopModelRoutes`、`apiKeyField` |

主界面同样分三段展示供应商；Claude Desktop 暂不支持面板内切换（点击卡片给出提示，
请在 CC Switch 中切换），「测连通」对三端都可用。

### 反代产品状态条（顶部，自动发现）

面板顶部**每个反代产品一条状态条**，数据全部来自兄弟项目的 `relay.json` 清单，
控制台代码不含任何具体产品的名字 / 端口 / 命令：

- **发现规则**：扫描本项目目录及其上两级目录下的每个子目录里的 `relay.json`
  （与本项目平级的兄弟项目各放各的清单）；exe 被挪到别处时，在 exe 旁放
  `controller.json` 写 `{"relay_roots": ["路径", …]}` 显式指定扫描根
- **状态条内容**：清单里的 `name` + `host:port` + 在线状态（绿色呼吸圆点 ● / 红色 ○）
- **离线 →「启动 / 修复反代」**：执行清单 `start.cmd`（`~` 与环境变量自动展开），
  等 `start.wait_seconds` 秒内端口就绪即算成功；命令输出走临时文件读取，
  规避常驻子进程继承管道导致的 EOF 挂起
- **在线 →「⏸ 暂停反代」**：只停监听 `port` 的进程，且**先校验命令行匹配清单
  `stop.cmdline_match` 正则才杀**（端口被陌生进程占用时拒绝操作）；重复点幂等
- **接入新产品 = 在兄弟目录放一份 relay.json，控制台零改动**

**relay.json 字段**（完整示例见 `../autoclaw-switch/README.md`「relay.json（反代清单）」）：

| 字段 | 说明 |
|---|---|
| `id` / `name` | 唯一 id（ASCII，API 路由用）/ 展示名（状态条文案） |
| `port` / `host` | 监听端口 / 健康检查地址（默认 `127.0.0.1`） |
| `start.cmd` | 启动命令的完整 argv（可省 → 不显示启动按钮） |
| `start.cwd` / `start.wait_seconds` | 工作目录 / 端口就绪等待秒数（默认 15） |
| `stop.cmdline_match` | 停止前校验监听进程命令行的正则（可省 → 不显示暂停按钮） |
| `health.type` | 健康检查方式，目前仅支持 `tcp` |

## 它不做什么

- **不支持官方 Codex / Claude 切换**（OAuth 登录流程超出本工具范围）
- **不修改已有 providers 配置**（新增供应商用面板内「＋ 添加供应商」表单，或跳 CC Switch 编辑）
- 不做 base_url 之外的深层健康检查（鉴权 / 模型可用性不在连通检测范围）

## 与 CC Switch 的关系

本工具**复用** CC Switch 数据库，不会破坏它。CC Switch GUI 仍可正常使用，
两边看到的当前供应商状态会保持一致。如果 CC Switch 在跑且锁了 DB，会自动
`SQLITE_BUSY` 重试（timeout=3s），提示「CC Switch 是否在跑？」。

## 测试状态

✅ Round-trip 测试通过（Claude / Codex 都从当前 → DeepSeek → 当前切换后完全恢复）：
- DB `is_current` 正确翻转
- `~/.claude/settings.json` / `~/.codex/auth.json` / `~/.codex/config.toml` 正确还原
- MCP servers 跨切换保留
- 自动生成 `.bak-pre-switch-*` 备份
- `~/.cc-switch/settings.json` `currentProvider*` 同步更新

✅ Web UI（2026-10-09）：`/api/state`、`/api/test`（真实 HTTP 探测）、页面 UTF-8、
Edge 应用窗口启动均实测通过；切换 API 与 tkinter 版共用同一套逻辑函数。

✅ 添加供应商（2026-10-09）：`POST /api/provider/add` 三端（codex / claude /
claude-desktop）实测写入结构与 CC Switch 一致（codex: auth/config/modelCatalog；
claude: env 五键 + meta 七键；desktop: env 两键 + claudeDesktopModelRoutes 含
labelOverride；均登记 provider_endpoints）；7 个校验分支正确返回 400/409；
测试行已删除并验证级联清理；三段列表、页签与 24+ 字段 headless 渲染无 JS 错误。

✅ 反代接入分离（2026-10-10）：控制台与反代项目拆成两个项目——`relay.json` 清单发现、
名字/端口/启停策略全部来自清单（代码零产品硬编码）；E2E 17/17（发现、清单字段、
停止→离线、启动→在线往返、未知 id 404、页面渲染、供应商计数）+ headless 渲染 7/7。
启动命令输出改走临时文件，规避 `close_fds=False` 子进程继承管道造成的 EOF 挂起。

✅ 打包 exe（2026-10-10）：PyInstaller onefile（无控制台 + 多尺寸图标），
exe 实测同一套 E2E 17/17，`/api/quit` 后进程干净退出、端口释放。

## 技术栈

- Python 3.13（WorkBuddy 托管），`http.server.ThreadingHTTPServer` 本地服务
- 界面：纯 HTML/CSS/JS 单文件，无框架、无 CDN、无外部字体/图片
- 窗口：Edge `--app=http://127.0.0.1:18777`（无地址栏/标签栏，近似原生窗口）；无 Edge 时回退默认浏览器
- 回退：`python webui.py --tk` 走原 tkinter 界面
- 打包：PyInstaller（仅构建期依赖）
- 标准库：sqlite3 / json / pathlib / re / http.server

**运行时 0 第三方依赖**。换电脑：装 exe 即用；或源码跑 Python 3.10+ 标准库即可。

## 已知限制

- 如果 CC Switch 在跑，**可能** 因为 SQLite 锁等 3 秒超时。
- Codex `[mcp_servers]` 段落保留是 regex 实现，理论上 Codex 0.149+ 改了 schema 可能漏
  极端边角情况（已用官方 CC Switch 的 live config 同款正则测试通过）。
- 不感知 proxy takeover 模式（CC Switch 接管反代时本工具可能误判）。
  如果你日常开代理接管，请关闭后再用本工具。
- `控制器.exe` 改了 `.py` / `controller.html` 源码后需要重新 `pyinstaller CONTROLLER.spec` 才生效；
  exe 被挪出项目目录时反代发现依赖同目录 `controller.json` 的 `relay_roots` 配置。
