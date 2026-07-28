# 竞品分析系统 — 前端开发文档

**日期**: 2026-07-27（全部完成）  
**风格**: PICO-8 暗色像素终端风（16 色调色板、零圆角、box-shadow 三段式像素边框）  
**技术栈**: 纯 HTML/CSS/JS + Vite 开发服务器，零框架零构建  
**项目路径**: `D:\AAAagent\projects\competitive-analysis-ui\`

---

## 一、架构决策

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 部署方式 | **前后端完全分离** | 独立项目、独立端口 |
| 前端地址 | `http://localhost:5173` | Vite 开发服务器 |
| 后端地址 | `http://localhost:8000` | FastAPI 默认端口 |
| 跨域方案 | 后端 CORS 中间件 | `allow_origins=["localhost:5173","127.0.0.1:5173"]` |
| API 封装 | `api.js` 统一管理 baseUrl + JWT | 切换环境只需改一处 |
| 路由方式 | Hash 路由 `#/dashboard` `#/task/xxx` | 纯前端，不依赖服务端重定向 |
| 字体 | Zpix（中文像素字体，CDN）+ Press Start 2P（标题） | 从不下载到本地 |
| 音效 | Web Audio API 合成方波 | 零外部文件依赖 |

---

## 二、实际项目结构（交付后）

```
D:\AAAagent\projects\competitive-analysis-ui\
├── index.html                  # SPA 入口
├── vite.config.js              # Vite 配置
├── package.json                # 仅 vite 一个依赖
├── FRONTEND_OVERVIEW.md        # 前端 API / 架构快速总览
├── CODEX_PHASE1.md             # Phase 1 Codex 提示词（17KB）
├── CODEX_PHASE2.md             # Phase 2 Codex 提示词（19KB）
├── CODEX_PHASE3.md             # Phase 3 Codex 提示词（16KB）
├── PHASE_SUMMARY.md            # 项目总结（简版）
│
└── src/
    ├── css/
    │   └── pixel.css            # PICO-8 像素风全局样式（16.7KB）
    │
    ├── js/
    │   ├── api.js               # HTTP 封装（GET/POST + SSE + JWT 自动挂载）
    │   ├── auth.js              # 鉴权（login/register/logout + token 管理）
    │   ├── router.js            # Hash 路由引擎（5 路由 + 鉴权守卫）
    │   └── sounds.js            # 8-bit Web Audio 方波音效引擎
    │
    └── pages/
        ├── login.js             # 登录/注册页（5.7KB）
        ├── dashboard.js         # 仪表盘（12.7KB，双模式+任务卡片网格）
        ├── task-detail.js       # 任务详情（9.8KB，SSE进度+终端日志）
        ├── report.js            # 报告页（11.9KB，评分+手写Markdown解析器）
        └── monitor.js           # 监控面板（13.5KB，6卡+双定时器）
```

每个 `pages/*.js` 导出三个函数：
```js
export function render(params)   // 返回 HTML 字符串
export function mount(params)    // 绑定事件 + 启动定时器/SSE
export function unmount()        // 清理定时器/SSE 连接
```

---

## 三、路由表

| hash | 页面 | 需鉴权 | 说明 |
|------|------|:--:|------|
| `#/login` | 登录注册 | ❌ | 默认重定向目标 |
| `#/dashboard` | 仪表盘 | ✅ | 默认首页，快速+高级双模式 |
| `#/task/:id` | 任务详情 | ✅ | SSE 进度条 + 终端日志双区 |
| `#/report/:taskId/:reportId` | 报告页 | ✅ | 评分 + Markdown 终端渲染 |
| `#/monitor` | 监控面板 | ✅ | 6 卡片 + 双 10s 定时轮询 |

---

## 四、各页面交付

### 4.1 登录/注册 `#/login`

- 居中 460px 像素卡片
- 标题 `╔══════════╗ · 竞品分析系统 · ╚══════════╝`
- 输入框 placeholder 用 `>` 提示符前缀
- 标签用 `[ 用户名 ]` `[ 密码 ]` 方括号风格
- 登录 + 注册 + 切换模式按钮
- 登录成功播放 `SFX.confirm()` → 跳转仪表盘

### 4.2 仪表盘 `#/dashboard`

- 左侧 220px 侧边栏：🔍 Logo + 导航项（▶ 仪表盘 / 监控面板）+ 用户信息 + 退出按钮
- 右侧上半区：`pixel-tabs` 切换快速/高级模式
  - **快速模式**：textarea + Ctrl+Enter 提交，POST `/api/tasks` body `{title, query}`
  - **高级模式**：标题 + 竞品 + 维度三个输入框，POST `/api/tasks` body `{title, competitors, dimensions}`
  - 提交成功 → 600ms 后跳 `#/task/{id}`
- 右侧下半区：任务卡片 3 列网格
  - 每张卡片：状态 badge + 标题 + 时间 + 竞品数
  - 状态映射：pending→等待中 / running→运行中 / completed→已完成 / failed→失败
  - 点击跳转 `#/task/{id}`
  - API.post 拦截 → 自动记录 task_id 到 localStorage

### 4.3 任务详情 `#/task/:id`

- 顶部导航栏：← 返回 + 标题 + 状态 badge
- 进度条区（pixel-box）：`pixel-progress` + `pixel-progress-fill`，宽度随 SSE `progress_pct` 实时变化
- 终端区（pixel-box，flex:1 撑满）：黑底绿字，日志行格式 `[HH:MM:SS] AGENT :: 消息`
  - Agent 颜色：COLLECTOR 黄 / ANALYZER 青 / WRITER 绿 / QUALITY 紫 / SUPERVISOR 琥珀 / SYSTEM 白
  - 错误日志红色
  - 自动滚动到底部，用户手动上滚暂停，滚回底恢复
- SSE 连接：`GET /api/tasks/{id}/stream`
  - `progress` 事件 → 追加日志 + 更新进度条
  - `complete` 事件 → 100% 进度 + "▸ 查看分析报告"按钮
  - `error` 事件 → 断开 3 秒后重连
- 已完成的 task → 直接查日志 + 显示报告按钮，不启 SSE
- unmount 关闭 EventSource

### 4.4 报告页 `#/report/:taskId/:reportId`

- 顶部导航栏：← 返回 + 标题 + 版本 badge（多版本时）
- 评分卡片：总分大号 `82/100` + 4 项细分指标小进度条（完整性/准确性/可读性/专业性）
- 报告正文：**手写逐行 Markdown 解析器**（零外部依赖）
  - H1 绿底分隔线 / H2 青色 / H3 黄色
  - 像素风格表格（`pixel-report-table`）
  - 列表项 marker 绿色
  - 行内代码 + 代码块
- 底部按钮：仪表盘 + 最新版本（多版本时）
- 无报告 → 空态："报告生成中，请稍后再来"

### 4.5 监控面板 `#/monitor`

- 顶部导航栏：← 仪表盘 + "监控面板" + 右侧运行状态灯（🟢/🟡/🔴）
- **第一行 3 张状态卡：**
  - 系统状态：OK / DEGRADED / UNREACHABLE（`GET /health.status`）
  - 数据库连接：CONNECTED / DISCONNECTED（`GET /health.db_connected`）
  - LLM 限流：60 RPM + 触发次数（`GET /metrics` → `ca_rate_limit_hits` 求和）
- **第二行 2 张指标卡：**
  - 任务吞吐：4 行进度条（pending/running/completed/failed，从 `ca_tasks_total` 分桶）
  - Agent 调用分布：5 行进度条（collector/analyzer/writer/quality/supervisor，从 `ca_agent_calls_total` 按 agent 聚合）
- **第三行安全阻断卡：** 4 行日志（whitelist/param_validation/rate_limit/pii，从 `ca_harness_blocks`）
- **底部状态栏：** `[ AUTO REFRESH · 10s ]` + `[ LAST: HH:MM:SS ]`
- 双定时器独立轮询 `/health` 和 `/metrics`，一个失败不影响另一个
- Prometheus 文本解析完全手写（`parsePrometheus()`），兼容多标签格式

---

## 五、共享模块

### 5.1 `pixel.css`（16.7KB）

设计方向：PICO-8 暗色像素终端风

- 16 色 CSS 变量（`--pixel-black` `--pixel-green` `--pixel-cyan` 等）
- Zpix 中文像素字体 + Press Start 2P 标题字体
- `.pixel-box` / `.pixel-card`：box-shadow 三段式像素边框（无 border-radius）
- `.pixel-btn` / `.pixel-btn-secondary` / `.pixel-btn-danger`：按压偏移效果
- `.pixel-input` / `.pixel-textarea`：聚焦绿色像素边框
- `.pixel-terminal`：黑底终端区（14px mono，line-height 1.8）
- `.pixel-report-*`：报告 H1/H2/H3/表格/列表/代码块
- `.pixel-score-*`：评分环形/分条
- `.pixel-card-title`：卡片标题（10px 大写 + 底部分割线）
- `.pixel-progress` + `.pixel-progress-fill`：条纹进度条（transition 0.3s ease）
- `.pixel-sidebar` / `.pixel-tabs` / `.pixel-badge` / `.pixel-spinner`
- 5 种 Agent 日志颜色（`.pixel-log-agent-collector/analyzer/writer/quality/system`）
- 动画：fade-in / slide-up / blink / spin / shake
- 全局 body：`image-rendering: pixelated` + `-webkit-font-smoothing: none`

### 5.2 `api.js`（3.0KB）

```js
API.get(path)        // GET + JWT 自动挂载 + 统一错误处理
API.post(path, body) // POST + JSON body
API.sse(path, {      // EventSource 封装
  onProgress,        // ← progress 事件
  onComplete,        // ← complete 事件
  onError,           // ← error 事件（兼容后端 SSE 事件和浏览器原生 onerror）
})
```

### 5.3 `auth.js`（2.1KB）

```js
Auth.login(username, password)      // POST /api/auth/login → 存 token
Auth.register(username, password)   // POST /api/auth/register → 存 token
Auth.logout()                       // 清 token → 跳 #/login
Auth.isLoggedIn()                   // 检查 token 存在性
Auth.getUsername() / Auth.getUserId() // 从 JWT payload 解析
```

### 5.4 `router.js`（3.8KB）

Hash 路由引擎，支持 `:param` 动态参数，5 条路由。
`init()` → 监听 `hashchange` → 匹配路由表 → `unmount()` 旧页面 → `render()` + `mount()` 新页面。
鉴权守卫：无 token 且页面需鉴权 → 跳 `#/login`。

### 5.5 `sounds.js`（2.6KB）

Web Audio API 方波合成，零文件依赖：
- `SFX.blip()` — 短促方波 440→880Hz，50ms（点击/tab切换）
- `SFX.confirm()` — 上行琶音 C5→E5→G5→C6 各 60ms（创建成功/任务完成）
- `SFX.error()` — 150Hz 低沉方波 300ms（错误）
- `SFX.click()` — 极轻 tap 1kHz 30ms（按钮）

---

## 六、配色板（实际使用）

```
PICO-8 暗色像素终端风

--pixel-black:  #0a0a0a    背景深黑
--pixel-dark:   #1a1c2c    面板底色
--pixel-mid:    #333c57    像素边框
--pixel-light:  #5a6988    次要文字
--pixel-white:  #e0e8f0    主文字
--pixel-cream:  #fff4d2    强调文字
--pixel-green:  #38b764    主强调/成功/终端文字
--pixel-green-dim: #265c42 进度条暗条纹
--pixel-green-dark: #19332f 绿色背景（badge/code/table-header）
--pixel-red:    #e43b44    错误/危险
--pixel-yellow: #f6c863    警告/Collector 日志
--pixel-blue:   #3b8eff    链接/info badge
--pixel-cyan:   #63c8f6    Analyzer 日志
--pixel-purple: #8968ba    Quality 日志
--pixel-amber:  #d29922    Supervisor 日志
```

---

## 七、后端依赖

全部复用已有端点，前端开发期间后端零改动（CORS 在 Phase 1 已加）：

| 端点 | 方法 | 鉴权 | 前端用途 |
|------|:--:|:--:|------|
| `/api/auth/register` | POST | ❌ | 注册 |
| `/api/auth/login` | POST | ❌ | 登录 |
| `/api/auth/me` | GET | ✅ | 获取当前用户 |
| `/api/tasks` | POST | ✅ | 创建任务 |
| `/api/tasks/{id}` | GET | ✅ | 查询任务状态 |
| `/api/tasks/{id}/stream` | GET | ✅ | SSE 进度推送 |
| `/api/tasks/{id}/reports` | GET | ✅ | 获取报告列表 |
| `/health` | GET | ❌ | 系统健康 |
| `/metrics` | GET | ❌ | Prometheus 指标 |

---

## 八、Codex 开发模式

三个 Phase 通过 Codex 子代理并行开发，每个 Phase 输出一份 10~17KB 详细提示词（`CODEX_PHASEx.md`），事后人工验收修复。

**各 Phase Bug 一览：**

| Phase | Bug | 修复 |
|:--:|------|------|
| 1 | 提交后不跳转任务详情 | `window.location.hash` 补上 |
| 1 | 所有输入框缺少 placeholder 示例 | 4 个 placeholder 全补 |
| 2 | SSE error 事件名 `error_srv` 后端不匹配 | 改为 `error` + data 空值区分 |
| 2 | Markdown 解析正则丢失 `$1` 捕获组 | 3 个 replace 全补 `$1` |
| 3 | Prometheus 解析正则写死单标签，无法匹配多标签 | `\{([^}]*)\}` 通配 + 按 agent 聚合求和 |

**常见 codex 错误模式：** 正则捕获组丢失、事件名称抄错、API 格式假设错误。

---

## 九、交付统计

| 类型 | 文件数 | 总大小 |
|------|:--:|------|
| 页面模块 | 5 | 47.6KB |
| 基础 JS | 4 | 11.5KB |
| CSS 样式 | 1 | 16.7KB |
| 入口 + 配置 | 3 | 1.1KB |
| Codex 提示词 | 3 | 51.9KB |
| 文档 | 2 | 5.3KB |
| **合计** | **18** | **~134KB** |
