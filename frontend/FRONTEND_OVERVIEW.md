# 竞品分析系统 — 前端总览（v2.0 · 像素风）

> **阅读对象：Codex Agent**
> 先通读本文档，了解项目架构和后端 API，再开始任何 Phase。

---

## 1. 项目定位

为竞品分析后端系统构建像素风 Web 前端。
- 后端：`D:\AAAagent\projects\competitive-analysis-system\`（FastAPI :8000）
- 前端：`D:\AAAagent\projects\competitive-analysis-ui\`（独立目录）
- 构建：Vite 5.x
- 框架：**无框架，原生 JS + Hash Router**
- 风格：**PICO-8 暗色像素终端风**

---

## 2. 文件结构

```
competitive-analysis-ui/
├── index.html                  # SPA 入口
├── package.json
├── vite.config.js              # proxy /api → :8000
├── src/
│   ├── css/pixel.css           # 像素风 CSS 基础库
│   ├── js/
│   │   ├── api.js              # fetch 封装 + JWT
│   │   ├── auth.js             # 鉴权守卫
│   │   ├── router.js           # Hash 路由引擎
│   │   └── sounds.js           # Web Audio 8-bit 音效
│   └── pages/
│       ├── login.js            # 登录/注册 [Phase 1]
│       ├── dashboard.js        # 仪表盘 [Phase 1]
│       ├── task-detail.js      # 任务详情 [Phase 2]
│       ├── report.js           # 报告页 [Phase 2]
│       └── monitor.js          # 监控面板 [Phase 3]
```

---

## 3. 路由表

| Hash | 页面 | 鉴权 | 参数 |
|------|------|:--:|------|
| `#/login` | login.js | 否 | — |
| `#/dashboard` | dashboard.js | 是 | — |
| `#/task/:id` | task-detail.js | 是 | id |
| `#/report/:taskId/:reportId` | report.js | 是 | taskId, reportId |
| `#/monitor` | monitor.js | 是 | — |

页面模块契约：`export function render(params)` / `export function mount(params)` / `export function unmount()`。

---

## 4. 后端 API

### 鉴权（/api/auth）

| 方法 | 路径 | Body | 响应 |
|------|------|------|------|
| POST | `/api/auth/register` | `{username, password}` | `{token, username, user_id}` |
| POST | `/api/auth/login` | `{username, password}` | `{token, username, user_id}` |
| GET | `/api/auth/me` | — | `{username, user_id}` |

### 任务（/api/tasks）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/tasks` | 创建任务 `{title, query?, competitors?, dimensions?}` |
| GET | `/api/tasks/{id}` | 任务详情 |
| GET | `/api/tasks/{id}/stream` | SSE 进度推送 |
| GET | `/api/tasks/{id}/reports` | 报告列表 |

### 系统

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/metrics` | Prometheus 指标 |

### 鉴权方式

除 login/register 外，所有 `/api/*` 需 `Authorization: Bearer <token>`。
SSE 的 token 通过 query string `?token=xxx` 传递。

---

## 5. 开发约定

1. 页面模块导出 `render` / `mount` / `unmount`
2. 所有 API 调用走 `API.get/post/sse`，不直接 fetch
3. token 管理走 `Auth`，不手写 localStorage
4. 文件编码 UTF-8，中文注释
5. 像素边框统一用 box-shadow 三段式模拟，不用 CSS border
6. 字体分层：Press Start 2P 用于标题/按钮/标签，Courier New/SimHei 用于正文/终端
7. 所有圆角 = 0
