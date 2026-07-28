# 竞品分析系统 — 前端项目总结

> 2026-07-27 | 3 Phase 全部完成

---

## 项目概况

```
competitive-analysis-ui/
├── index.html                524B   入口（Vite 挂载点 + CSS/JS 引用）
├── package.json               298B   依赖（仅 vite）
├── vite.config.js             298B   Vite 配置
├── CODEX_PHASE1.md          17.2KB   Phase 1 Codex 提示词
├── CODEX_PHASE2.md          18.6KB   Phase 2 Codex 提示词
├── CODEX_PHASE3.md          16.0KB   Phase 3 Codex 提示词
├── FRONTEND_OVERVIEW.md      3.2KB   前端架构总览
└── src/
    ├── css/
    │   └── pixel.css         16.7KB   PICO-8 暗色像素风全局样式
    ├── js/
    │   ├── api.js             3.0KB   HTTP 封装（GET/POST + SSE）
    │   ├── auth.js            2.1KB   JWT 鉴权（login/register/logout）
    │   ├── router.js          3.8KB   Hash 路由引擎（5 路由）
    │   └── sounds.js          2.6KB   Web Audio 方波音效引擎
    └── pages/
        ├── login.js           5.7KB   登录/注册页
        ├── dashboard.js      12.7KB   仪表盘（创建任务+任务队列）
        ├── task-detail.js     9.8KB   任务详情（SSE进度+终端日志）
        ├── report.js         11.9KB   报告页（评分+Markdown渲染）
        └── monitor.js        13.5KB   监控面板（6卡+双定时轮询）
```

## 各 Phase 交付

| Phase | 内容 | 文件 | Bug 修复 |
|:--:|------|------|------|
| 1 | 登录改造 + 仪表盘 | `login.js` `dashboard.js` `pixel.css` | 提交后无跳转、placeholder 示例缺失 |
| 2 | 任务详情 + 报告页 | `task-detail.js` `report.js` | SSE error_srv→error、parseInline $1 丢失 |
| 3 | 监控面板 | `monitor.js` | Prometheus 多标签解析正则写死单标签 |

## 核心设计决策

- **风格：** PICO-8 暗色像素终端风，16 色调色板，零圆角，box-shadow 三段式像素边框
- **路由：** Hash 路由（`#/login` `#/dashboard` `#/task/:id` `#/report/:taskId/:reportId` `#/monitor`）
- **渲染契约：** 每个页面 `render()` → `mount()` → `unmount()`
- **SSE：** 事件源连接，Task 详情页双区并行（进度条+终端日志），页面卸载关闭连接
- **监控：** 手写 Prometheus text 解析器，`/health` 和 `/metrics` 双定时器独立 10s 轮询
- **报告：** 手写逐行 Markdown 解析器，不引入任何库

## Codex 协作模式

三阶段使用 Codex 子代理开发，每 Phase 提供 10-12KB 详细提示词（CODEX_PHASEx.md），事后人工验收修复。常见错误类型：正则捕获组丢失、事件名称抄错、多标签格式假设错误。

## 后端对接

全部复用已有端点，未新增任何后端代码：
- `POST /api/auth/register` `POST /api/auth/login` `GET /api/auth/me`
- `POST /api/tasks` `GET /api/tasks/{id}` `GET /api/tasks/{id}/stream` `GET /api/tasks/{id}/reports`
- `GET /health` `GET /metrics`
