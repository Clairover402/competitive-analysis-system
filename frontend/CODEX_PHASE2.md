# Phase 2 — 任务详情页 + 报告页（像素风）

> **Codex Prompt · 2026-07-27**
> 先读 `FRONTEND_OVERVIEW.md` 了解架构和 API
> 执行目录：`D:\AAAagent\projects\competitive-analysis-ui\`

---

## 前提

Phase 1 已交付：`pixel.css`（14KB，PICO-8 暗色像素风）、`login.js`、`dashboard.js`、`api.js`、`auth.js`、`router.js`、`sounds.js`。所有 CSS 类名带 `pixel-` 前缀。

---

## 任务目标

新建两个页面模块，完成「创建任务 → 看进度滚日志 → 点报告看结果」闭环。

| # | 文件 | 功能 |
|---|------|------|
| 1 | `src/pages/task-detail.js` | 任务详情：双区布局（进度条 + 终端日志），SSE 实时 |
| 2 | `src/pages/report.js` | 报告页：质量评分 + 全量分析报告 |

---

## 1. task-detail.js — 任务详情页

### 1.1 布局

```
┌─────────────────────────────────────────────────────────────────┐
│  ← 返回  任务标题                              [状态 BADGE]      │  ← 顶部导航栏
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ╔════════════════════════════════════════════════════════════╗ │
│  ║  ████████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░  62%      ║ │  ← 进度条区（pixel-box）
│  ║  Collector 采集完成 → Analyzer 分析中...                    ║ │  ← 当前阶段文字
│  ╚════════════════════════════════════════════════════════════╝ │
│                                                                  │
│  ╔════════════════════════════════════════════════════════════╗ │
│  ║  SYSTEM LOG                                     [AUTO ▾]  ║ │  ← 终端区（pixel-box）
│  ║  ───────────────────────────────────────────────────────── ║ │
│  ║  [16:42:01] COLLECTOR :: 正在搜索竞品相关信息...          ║ │
│  ║  [16:42:03] COLLECTOR :: 正在抓取数据源...                ║ │
│  ║  [16:42:05] ANALYZER  :: 正在多维度分析竞品...            ║ │
│  ║  [16:42:08] WRITER    :: 正在生成分析报告...              ║ │
│  ║  [16:42:11] QUALITY   :: 正在质检报告...                  ║ │
│  ║                                                           ║ │
│  ║  [16:42:15] █ SYSTEM :: 任务完成 (耗时 45s)               ║ │
│  ║  [16:42:15] █ SYSTEM :: 质量评分: 82/100                  ║ │
│  ║                                                           ║ │
│  ║  >>> ┌──────────────────────────────────┐                ║ │
│  ║  >>> │  ▸ 查看分析报告                   │                ║ │  ← 完成后出现
│  ║  >>> └──────────────────────────────────┘                ║ │
│  ║                                                           ║ │
│  ╚════════════════════════════════════════════════════════════╝ │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 顶部导航栏

- 全宽，`pixel-dark` 背景，底部 `2px solid pixel-mid` 分割线
- 左侧：`← 返回` 按钮（`pixel-btn-secondary`），点击 `window.location.hash = '#/dashboard'`
- 中间：任务标题（`pixel-font`，`pixel-green`，14px）
- 右侧：状态 badge（使用 `pixel-badge-*`，与仪表盘同映射）
- 高度固定 56px

### 1.3 进度条区

一个 `.pixel-box`：
- 上方：`.pixel-progress` + `.pixel-progress-fill`，宽度随 `progress_pct` 实时变化
- 进度条右侧显示百分比数字（`pixel-font-mono`，`pixel-green`，14px）
- 下方一行：当前正在执行的阶段名称（从 SSE 的最后一条 progress 事件取 `message` 字段），`pixel-mono` 14px，`pixel-text-light`

### 1.4 终端区

一个 `.pixel-box`，高度自适应撑满下方空间（`flex:1`，overflow-y: auto）。

**终端头部：**
- 标题 "SYSTEM LOG"（`pixel-font` 10px，`pixel-text-light`，大写）
- 右侧 "AUTO" 标签（`pixel-badge-info`），表示自动滚动（滚动到底部=开启，用户手动上滚=暂停自动滚）

**日志行格式：**
```
[HH:MM:SS] AGENT_NAME :: 消息内容
```
- 时间戳 `pixel-text-light` 12px
- Agent 名用 `pixel-font` 9px 大写：
  - `COLLECTOR` → `pixel-text-yellow`
  - `ANALYZER` → `pixel-text-cyan`
  - `WRITER` → `pixel-text-green`
  - `QUALITY` → `pixel-text-purple`（用变量 `--pixel-purple: #8968ba`）
  - `SUPERVISOR` → `pixel-text-amber`
  - `SYSTEM` → `pixel-text-white`
- 分隔符 `::` `pixel-text-light`
- 消息内容 `pixel-mono` 14px：
  - 正常消息 → `pixel-green`
  - 错误消息 → `pixel-red`
  - 系统消息（完成/失败） → `pixel-green` 或 `pixel-red`，前面加 `█` 前缀

**自动滚动逻辑：**
- 默认开启自动滚动（新日志到来 → `scrollTop = scrollHeight`）
- 用户手动往上滚 → 关闭自动滚动，AUTO 标签变灰
- 用户滚回底部 → 恢复自动滚动，AUTO 标签恢复绿色
- 用 `scroll` 事件监听，在用户手动滚动和自动滚动间区分（用标志位）

### 1.5 SSE 连接管理

**`mount()` 中：**
1. 先 `GET /api/tasks/{id}` 获取任务当前状态
2. 如果状态已是 `completed` → 直接显示完整进度条 + 查询日志一次 + 显示"查看报告"按钮
3. 如果状态已是 `failed` → 显示失败信息 + "返回仪表盘"按钮
4. 否则 → 启动 SSE 连接 `API.sse('/api/tasks/{id}/stream', handlers)`

**SSE handlers：**
```js
{
  onMessage(msg) {
    // msg.event === 'progress' → 追加日志行 + 更新进度条 + 更新阶段文字
    // 注意：msg 的结构是 { event: 'progress', data: '{...}' }
    //      数据在 msg.data 中，需要 JSON.parse
  },
  onDone(msg) {
    // msg.event === 'complete' → 绿色 SYSTEM 日志 + 进度条 100% + 显示"查看报告"按钮
    // msg.data: { report_id, quality_score, elapsed_ms }
  },
  onError(msg) {
    // msg.event === 'error' → 红色 SYSTEM 日志 + 停止 SSE
  },
}
```

**`unmount()` 中：**
- 关闭 EventSource（`es.close()`）

### 1.6 状态机

页面有 4 个视觉状态：

| 状态 | 进度条 | 终端 | 按钮区 |
|------|--------|------|--------|
| **loading** | 显示 spinner | 显示 "> 正在连接任务..." | 无 |
| **running** | 实时更新 | 实时追加日志 | 无 |
| **completed** | 100% 绿色 | 最后一条 "█ SYSTEM :: 任务完成 (耗时 XXs)" | "▸ 查看分析报告" 按钮 |
| **failed** | 停在当前位置 | 红色错误日志 | "返回仪表盘" 按钮 |

### 1.7 错误/边界

- 任务不存在（404）→ 居中显示 "任务不存在" + "返回仪表盘" 按钮
- SSE 连接中断 → 最后一条日志标红 "连接中断，正在重连..."，3 秒后重试
- API.get 失败 → 同 404 处理
- report_id 为 null/undefined → "查看报告" 按钮不显示（罕见但可能）

---

## 2. report.js — 报告页

### 2.1 布局

```
┌─────────────────────────────────────────────────────────────────┐
│  ← 返回  报告标题                          版本 v3 · 最新      │  ← 顶部导航栏
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ╔════════════════════════════════════════════════════════════╗ │
│  ║  质量评分: [████████░░] 82/100                             ║ │  ← 评分卡片（pixel-box）
│  ║                                                           ║ │
│  ║  完整性: 85  ·  准确性: 78  ·  可读性: 90  ·  时效性: 72  ║ │  ← 细分指标（4 列）
│  ╚════════════════════════════════════════════════════════════╝ │
│                                                                  │
│  ╔════════════════════════════════════════════════════════════╗ │
│  ║                                                            ║ │  ← 报告正文区（pixel-box）
│  ║  # 竞品分析报告                                            ║ │
│  ║                                                            ║ │
│  ║  ## 概述                                                   ║ │
│  ║  本次分析了...                                              ║ │
│  ║                                                            ║ │
│  ║  ## 玩法设计                                                ║ │
│  ║  ...                                                       ║ │
│  ║                                                            ║ │
│  ║  （Markdown 渲染为像素终端风格）                              ║ │
│  ║                                                            ║ │
│  ╚════════════════════════════════════════════════════════════╝ │
│                                                                  │
│  ┌────────────┐  ┌────────────┐                                 │
│  │  ▸ 仪表盘   │  │  ▸ 最新版  │                                 │  ← 底部操作按钮
│  └────────────┘  └────────────┘                                 │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 顶部导航栏

- 同 task-detail 的导航栏样式
- 左侧：`← 返回` → `window.location.hash = '#/task/{taskId}'`
- 中间：报告标题（取 task.title + "分析报告"）
- 右侧：版本号 + `pixel-badge-info` "最新"（仅 `is_latest=true` 时显示）

### 2.3 质量评分区

从 `GET /api/tasks/{taskId}/reports` 的 `quality_score` 和 `quality_details` 字段取值。

`quality_details` 结构（来自后端，5 个 LLM-as-Judge 维度）：
```json
{
  "completeness": 85,
  "accuracy": 78,
  "readability": 90,
  "timeliness": 72,
  "overall": 82
}
```

**评分条：**
- 总分大号显示：`pixel-font-display` 24px，`pixel-green`，"82/100"
- 左侧用 `.pixel-progress` + `.pixel-progress-fill` 展示（width = overall%）

**细分指标：**
- 4 个指标横排（用 `pixel-flex` + `pixel-gap-md`）
- 每个指标：标签（`pixel-text-light` 11px）+ 分数（`pixel-green` 16px `pixel-font-display`）+ 小进度条

**边界：**
- `quality_score` 为 null → 显示 "未评估"（`pixel-text-light`）
- `quality_details` 为 null → 不显示细分指标

### 2.4 报告正文区

**渲染 Markdown 为像素终端风格：**

不需要引入 Markdown 解析库。自己手写一个极简 Markdown → HTML 转换函数（`_renderMarkdown(content)`）：

```
# 标题       → <h1 class="pixel-report-h1">
## 标题      → <h2 class="pixel-report-h2">
### 标题     → <h3 class="pixel-report-h3">
**粗体**     → <strong>
*斜体*       → <em>
- 列表项     → <li>
1. 有序列表  → <ol><li>
空行         → <br>
表格（| a | b |）→ <table> 像素风格表格
```

注意：**不要用 innerHTML 拼接正则**——用逐行解析。按 `\n` 分割，一行一行处理，维护 `inTable` / `inList` 状态。

**像素终端风格样式（在报告中定义内联 `<style>`）：**

```css
/* 报告正文专用 */
.pixel-report-h1 {
  font-family: var(--pixel-font-display);
  font-size: 16px;
  color: var(--pixel-green);
  margin: 24px 0 12px;
  padding-bottom: 8px;
  border-bottom: 2px solid var(--pixel-mid);
  letter-spacing: 3px;
  text-transform: uppercase;
}
.pixel-report-h2 {
  font-family: var(--pixel-font);
  font-size: 14px;
  color: var(--pixel-cyan);
  margin: 20px 0 10px;
  letter-spacing: 2px;
}
.pixel-report-h3 {
  font-family: var(--pixel-font);
  font-size: 12px;
  color: var(--pixel-yellow);
  margin: 16px 0 8px;
  letter-spacing: 1px;
}
.pixel-report-table {
  width: 100%;
  border-collapse: collapse;
  margin: 12px 0;
}
.pixel-report-table th {
  background: var(--pixel-green-dark);
  color: var(--pixel-green);
  font-family: var(--pixel-font);
  font-size: 11px;
  padding: 8px 12px;
  border: 1px solid var(--pixel-mid);
  text-align: left;
  letter-spacing: 1px;
}
.pixel-report-table td {
  padding: 8px 12px;
  border: 1px solid var(--pixel-mid);
  font-family: var(--pixel-mono);
  font-size: 14px;
}
.pixel-report-list {
  padding-left: 24px;
  margin: 8px 0;
}
.pixel-report-list li {
  font-family: var(--pixel-mono);
  font-size: 14px;
  line-height: 1.8;
  color: var(--pixel-white);
}
.pixel-report-list li::marker {
  color: var(--pixel-green);
}
```

### 2.5 底部操作按钮

- 左侧：`pixel-btn-secondary` "◀ 仪表盘" → `#/dashboard`
- 右侧：`pixel-btn-secondary` "▸ 最新版本" → 重新加载（`window.location.reload()`），仅当有多版本时显示

### 2.6 数据加载

`mount({ taskId, reportId })` 中：
1. `GET /api/tasks/{taskId}` → 获取 task 信息（用于标题）
2. `GET /api/tasks/{taskId}/reports` → 获取报告列表
3. 如果 URL 中指定了 `reportId` → 从列表中找对应版本
4. 否则取 `is_latest=true` 的那份（或列表第一份）
5. 如果报告列表为空 → 显示 "暂无报告" + "返回任务详情" 按钮
6. 如果 task 不存在 → 显示 "任务不存在"

### 2.7 错误/边界

- 全部接口错误 → 显示错误消息 + "返回仪表盘" 按钮
- 报告 content 为空/null → 显示 "报告生成中..."
- quality_score 为 null → 评分区显示 "未评估"
- quality_details 为 null 或缺少字段 → 对应的细分指标不渲染

---

## 3. api.js 注意事项

SSE 返回的事件数据结构：
```json
// progress 事件
{"event": "progress", "data": "{\"agent\":\"collector\",\"action\":\"web_search\",\"message\":\"正在搜索...\",\"progress_pct\":0.25}"}

// complete 事件
{"event": "complete", "data": "{\"task_id\":\"xxx\",\"status\":\"completed\",\"report_id\":\"yyy\",\"quality_score\":82,\"elapsed_ms\":45200}"}

// error 事件
{"event": "error", "data": "{\"task_id\":\"xxx\",\"message\":\"任务执行失败\",\"elapsed_ms\":30000}"}
```

`API.sse()` 返回原生 EventSource 对象。`onmessage` 收到的 `event` 不是 `MessageEvent.data`——需要注意 EventSource 的 `addEventListener` 机制。当前 `api.js` 的 `onmessage` handler 已经接收的是 JSON 解析后的整个事件对象（包含 `type` 和 `data` 字段），需要兼容。

---

## 4. 交互细节

1. 终端日志区自动滚动到底部（用户手动上滚时暂停，滚回底部后恢复）
2. 进度条过渡用 CSS `transition: width 0.3s ease`
3. SSE 连接中断时在终端显示 "█ SYSTEM :: 连接中断，3 秒后重连..."，然后重新建立连接
4. 报告页的 Markdown 渲染用逐行解析，不导入外部库
5. 两个页面都使用 `render()` / `mount()` / `unmount()` 契约
6. 页面卸载时关闭 SSE 连接，防止内存泄漏
7. 按钮点击播放 `SFX.click()`，完成/错误播放 `SFX.confirm()` / `SFX.error()`

---

## 5. 验收自检

**task-detail.js：**
- [ ] 从仪表盘创建任务 → 跳转任务详情页
- [ ] SSE 连接建立 → 终端逐行新增日志
- [ ] 进度条随 progress_pct 更新
- [ ] 不同 agent 的日志行颜色不同（collector 黄 / analyzer 青 / writer 绿 / quality 紫）
- [ ] 任务完成 → 进度条 100% → 出现"查看分析报告"按钮
- [ ] 点"查看报告" → 跳 `#/report/{taskId}/{reportId}`
- [ ] 点返回 → 回仪表盘
- [ ] 自动滚动工作，手动上滚暂停自动滚动
- [ ] SSE 断开重连
- [ ] 页面卸载后 SSE 连接关闭

**report.js：**
- [ ] 从任务详情点"查看报告" → 跳转报告页
- [ ] 质量评分正确显示
- [ ] 细分指标 4 项显示（如果数据存在）
- [ ] Markdown 渲染为像素终端风格
- [ ] 标题层级颜色不同（h1 绿 / h2 青 / h3 黄）
- [ ] 表格像素风格渲染
- [ ] 底部按钮正确跳转
- [ ] 返回按钮回任务详情页
- [ ] 无报告时显示空态

---

## 6. 文件清单

| 文件 | 操作 | 预期大小 |
|------|------|---------|
| `src/pages/task-detail.js` | 新建 | ~12KB |
| `src/pages/report.js` | 新建 | ~10KB |
| `src/css/pixel.css` | **不修改** | 已有 |
| `src/js/*` | **不修改** | 已有 |
| `src/pages/login.js` | **不修改** | 已有 |
| `src/pages/dashboard.js` | **不修改** | 已有 |
