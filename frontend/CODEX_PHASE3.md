# Phase 3 — 监控面板（像素风）

> **Codex Prompt · 2026-07-27**
> 先读 `FRONTEND_OVERVIEW.md` 了解架构和 API
> 执行目录：`D:\AAAagent\projects\competitive-analysis-ui\`

---

## 前提

Phase 1+2 已交付：`pixel.css`（16.5KB）、`login.js`、`dashboard.js`、`task-detail.js`、`report.js`、`api.js`、`auth.js`、`router.js`、`sounds.js`。路由表已有 `/monitor` 入口。

**你不能新建后端端点** —— 只在前端消费已有数据。

---

## 任务目标

新建 `src/pages/monitor.js`，完成一个实时监控仪表盘。

只能打两个端点：
| 端点 | 返回格式 | 用途 |
|------|---------|------|
| `GET /health` | `{"status":"ok"\|"degraded","db_connected":true\|false}` | 系统健康+DB连通 |
| `GET /metrics` | Prometheus text（见 §2） | 7 项业务指标 |

---

## 1. 页面布局

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  ← 仪表盘    ╎ 监控面板                                        ╎ 运行中 🟢  │  ← 顶部导航栏
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐          │
│  │  系统状态        │  │  数据库连接      │  │  LLM 限流        │          │  ← 3 张状态卡
│  │  ✅ OK          │  │  ✅ 已连接       │  │  60 RPM / 0 触发 │          │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘          │
│                                                                              │
│  ┌─────────────────────────────────────┐  ┌──────────────────────────────┐  │
│  │  任务吞吐（最近刷新）                │  │  Agent 调用分布              │  │  ← 2 张指标卡
│  │                                     │  │                              │  │
│  │  pending     0  ████████░░░░░░░░   │  │  collector   ████████████ 12  │  │
│  │  running     1  ████████░░░░░░░░   │  │  analyzer    ██████████   10  │  │
│  │  completed  42  ████████████████   │  │  writer      ██████        6  │  │
│  │  failed      3  ██░░░░░░░░░░░░░░   │  │  quality     ██████        6  │  │
│  │                                     │  │  supervisor  ████████      8  │  │
│  └─────────────────────────────────────┘  └──────────────────────────────┘  │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────────┐│
│  │  安全阻断  · 最近 5 条事件                                               ││  ← 安全事件日志
│  │  [18:03:11] whitelist 阻断: tool=web_search, agent=analyzer              ││
│  │  [17:58:42] param_validation 阻断: token 超长                            ││
│  │  [17:45:09] rate_limit 阻断: agent=collector, RPM=61/60                  ││
│  │  ...                                                                    ││
│  └──────────────────────────────────────────────────────────────────────────┘│
│                                                                              │
│  ╎  AUTO REFRESH · 10s                                          ╎           │  ← 底部状态栏
└──────────────────────────────────────────────────────────────────────────────┘
```

### 1.1 顶部导航栏
- 同 Phase 2 的导航栏样式（56px 高，`pixel-dark` 背景，底部分割线）
- 左侧：`← 仪表盘` 按钮 → `#/dashboard`
- 中间："监控面板"（`pixel-font-display` `pixel-green` 15px）
- 右侧：系统运行状态指示灯 `🟢 运行中` 或 `🔴 异常`（来自 `/health`，10 秒刷新一次）

### 1.2 底部状态栏
- 全宽，`pixel-dark` 背景，顶部分割线，高度 36px
- 左侧：`[ AUTO REFRESH · 10s ]`（`pixel-font` 9px `pixel-text-light`，方括号包裹，大写）
- 右侧：`[ LAST: HH:MM:SS ]`（显示上次成功拉取的时间）

---

## 2. /metrics 端点解析

### 2.1 Prometheus 文本格式

`GET /metrics` 返回 `text/plain`，格式：

```
# HELP ca_tasks_total 竞品分析任务总数
# TYPE ca_tasks_total counter
ca_tasks_total{status="pending"} 2.0
ca_tasks_total{status="running"} 1.0
ca_tasks_total{status="completed"} 42.0
ca_tasks_total{status="failed"} 3.0
# HELP ca_task_duration_seconds 任务执行耗时（秒），按路由类型分桶
# TYPE ca_task_duration_seconds histogram
ca_task_duration_seconds_bucket{route="pipeline",le="1.0"} 5.0
ca_task_duration_seconds_bucket{route="pipeline",le="5.0"} 12.0
...
ca_task_duration_seconds_count{route="pipeline"} 38.0
ca_task_duration_seconds_sum{route="pipeline"} 1856.0
# HELP ca_agent_calls_total Agent 调用次数
# TYPE ca_agent_calls_total counter
ca_agent_calls_total{agent="collector",action="web_search",status="success"} 12.0
ca_agent_calls_total{agent="collector",action="web_fetch",status="success"} 8.0
ca_agent_calls_total{agent="analyzer",action="analyze",status="success"} 10.0
ca_agent_calls_total{agent="writer",action="write",status="success"} 6.0
ca_agent_calls_total{agent="quality",action="grade",status="success"} 6.0
ca_agent_calls_total{agent="supervisor",action="think",status="success"} 8.0
# HELP ca_rate_limit_hits 限流触发次数
# TYPE ca_rate_limit_hits counter
ca_rate_limit_hits{layer="global",agent=""} 0.0
ca_rate_limit_hits{layer="agent",agent="collector"} 0.0
ca_rate_limit_hits{layer="llm",agent="analyzer"} 1.0
# HELP ca_harness_blocks Harness 安全检查阻断次数
# TYPE ca_harness_blocks counter
ca_harness_blocks{check_type="whitelist"} 5.0
ca_harness_blocks{check_type="param_validation"} 3.0
ca_harness_blocks{check_type="rate_limit"} 2.0
ca_harness_blocks{check_type="pii"} 1.0
```

### 2.2 解析规则

写一个 `parsePrometheus(text)` 函数，按行解析：

1. 跳过 `#` 开头的 HELP/TYPE 行
2. 匹配 `metric_name{label1="v1",label2="v2"} value` 格式
3. 跳过 `_bucket` / `_count` / `_sum` 后缀（histogram 子指标，Phase 3 不用）
4. 只提取 Counter 和 Gauge 类型的值（`_total` 后缀的 Counter、无后缀的 Gauge）

**解析结果结构：**
```js
{
  "ca_tasks_total": {
    'pending': 2, 'running': 1, 'completed': 42, 'failed': 3
  },
  "ca_agent_calls_total": {
    // {agent}/{action}/{status} → count，下面演示合计，但 store 原始数据
  },
  "ca_rate_limit_hits": { /* ... */ },
  "ca_harness_blocks": { /* ... */ },
  "ca_quality_score_count": 6,  // 质检次数（count 是 histogram 唯一有用的汇总值）
  "ca_task_duration_seconds_count": 42  // 任务数
}
```

**解析正则：**
```js
// 有效的指标行（跳过 HELP/TYPE 和 histogram 子指标）
var m = line.match(/^(\w+)\{([^}]+)\}\s+([\d.e+\-]+)$/);
// m[1] = 指标名, m[2] = label 字符串, m[3] = 数值

// 解 label：
// labels.split(',') → ['agent="collector"', 'action="web_search"', 'status="success"']
// 每个 trim → replace(/(\w+)="([^"]*)"/g, ...)
```

---

## 3. 三张状态卡（第一行）

3 列等宽（`pixel-flex` + `flex:1` + `pixel-gap-md`），每个卡用 `.pixel-box`。

### 3.1 系统状态卡

标题：`STATUS`（`pixel-font` 10px `pixel-text-light` 大写）

内容：
- 大号图标 + 文字：
  - `status === "ok"` → 🟢（或绿色方块） `OK`（`pixel-green` `pixel-font-display` 18px）
  - `status === "degraded"` → 🟡 `DEGRADED`（`pixel-yellow`）
  - 请求失败 → 🔴 `UNREACHABLE`（`pixel-red`）
- 底部小字：`[UPTIME: —]`（uptime 不实现，固定 `—`）

### 3.2 数据库连接卡

标题：`DATABASE`

内容：
- `db_connected === true` → 🟢 `CONNECTED`（绿字）
- `db_connected === false` → 🔴 `DISCONNECTED`（红字）
- 请求失败 → 🔴 `UNKNOWN`（红字）

### 3.3 LLM 限流卡

标题：`LLM RATE`

内容（从 `/metrics` 的 `ca_rate_limit_hits` 和 `ca_agent_calls_total` 聚合）：
- 上限：固定 `60 RPM`（系统配置）
- 触发次数：`ca_rate_limit_hits` 各 layer 总和
  - 0 次触发 → `0 次触发`（`pixel-green`）
  - ≥1 次触发 → `X 次触发`（`pixel-red`）

---

## 4. 两张指标卡（第二行）

两列 `1:1`，用 `.pixel-box`。

### 4.1 任务吞吐卡

标题：`TASK THROUGHPUT`

从 `ca_tasks_total` 取 4 个 status 的值，每个一行：
```
pending    ████░░░░░░░░  0
running    ████░░░░░░░░  1
completed  ████████████  42
failed     ██░░░░░░░░░░  3
```

- 每行：status 名称（`pixel-font` 10px 大写，`pixel-text-light`）+ 进度条（`pixel-progress` + `pixel-progress-fill`，宽度 = 该状态值 / 总任务数 × 100%）+ 数值（`pixel-font-mono` 14px `pixel-green`）
- 总任务数 = pending + running + completed + failed（0 时进度条 0%）
- 颜色映射：pending=`pixel-yellow`，running=`pixel-blue`，completed=`pixel-green`，failed=`pixel-red`（仅 bar 颜色，数值保持绿色）

### 4.2 Agent 调用分布卡

标题：`AGENT CALLS`

从 `ca_agent_calls_total` 按 agent 维度聚合（sum 同一 agent 的不同 action/status）：

```
collector   ████████████████████  20
analyzer    ██████████████        14
writer      ██████                6
quality     ██████                6
supervisor  ██████████            10
```

- 每行：agent 名（`pixel-font` 9px 大写）+ 进度条（宽度 = 该 agent / maxCall）× 100%）+ 调用次数
- agent 颜色：collector=`var(--pixel-yellow)`，analyzer=`var(--pixel-cyan)`，writer=`var(--pixel-green)`，quality=`var(--pixel-purple)`，supervisor=`var(--pixel-amber)`
- maxCall = 5 个 agent 中最大的调用次数

---

## 5. 安全事件日志卡（第三行）

标题：`HARNESS BLOCKS`

从 `ca_harness_blocks` 取各 check_type 的累计值，以日志格式展示：

```
[NOW] whitelist          阻断 5 次    > 工具白名单拦截
[  -] param_validation   阻断 3 次    > 参数校验不通过
[  -] rate_limit         阻断 2 次    > Agent 级频控触发
[  -] pii               阻断 1 次    > 敏感信息泄露拦截
```

- 每行格式：`[状态] check_type  阻断 X 次    > 说明文字`
- 状态 `[NOW]`（绿色）表示该类型有阻断，`[-]`（灰色）表示 0 次
- check_type 用 `pixel-font` 10px 大写 `pixel-green`
- 阻断次数用 `pixel-font-mono` 14px
- 说明文字用 `pixel-mono` 13px `pixel-text-light`

**check_type → 中文说明映射：**
```
whitelist         → 工具白名单拦截
param_validation  → 参数校验不通过
rate_limit        → Agent 级频控触发
pii               → 敏感信息泄露拦截
```

---

## 6. 轮询刷新机制

### 6.1 定时器

`mount()` 中启动两个独立的 `setInterval`：

| 对象 | 端点 | 间隔 | 原因 |
|------|------|------|------|
| Health | `/health` | 10s | 轻量查询，快速感知异常 |
| Metrics | `/metrics` | 10s | 同频刷新，简单 |

两个定时器独立——一个失败不影响另一个。

### 6.2 数据加载

1. 先各发一次请求获取初始数据
2. 然后每 10 秒刷新

**promise 失败处理：**
- `/health` 失败 → 系统状态卡变 `UNREACHABLE`（红），DB 卡变 `UNKNOWN`
- `/metrics` 失败 → 保留上一次成功的数据不变，只在底部状态栏标红 `[ FETCH · FAILED ]`

### 6.3 底部状态栏

- 左侧清空间隔显示：`[ AUTO REFRESH · 10s ]`
- 右侧时间显示上次成功：`[ LAST: HH:MM:SS ]`
- 轮询失败时右侧变红：`[ FETCH · FAILED ]`

### 6.4 清理

`unmount()` 中 `clearInterval` 两个定时器。

---

## 7. 渲染契约

```
export function render()     → 返回 HTML 字符串
export function mount()      → 绑定事件 + 启动定时器 + 首次拉取数据
export function unmount()    → 清理定时器
```

`router.js` 已有 `/monitor` → `monitor.js` 的路由映射，不需要新增。

---

## 8. 边界与错误

| 场景 | 处理 |
|------|------|
| `/health` 返回非 200 | 状态卡红字 `UNREACHABLE` |
| `/metrics` 返回非 200 | 保留上次数据，状态栏 `FETCH · FAILED` |
| `ca_tasks_total` 全部为 0 | 任务吞吐卡显示 4 行空进度条 + 0 |
| `ca_agent_calls_total` 全部为 0 | Agent 调用卡显示 5 行空进度条 + 0 |
| `ca_rate_limit_hits` 不存在 | LLM 限流卡显示 `0 次触发`（绿色） |
| Prometheus 文本解析失败 | 保留上次数据不变 |
| 所有 cards 的 title 用 `<p class="pixel-card-title">`（写在 pixel.css 里，`pixel-font` 10px `pixel-text-light` 大写，`margin-bottom:14px`，底部分割线） |

---

## 9. 交互细节

1. 每 10 秒拉取时播放 `SFX.blip()`（极轻提示音，不吵）
2. 指标卡里的进度条用 CSS transition `width 0.4s ease`
3. 状态 `degraded` 时右上角状态灯变黄 `🟡`
4. 首次加载时各卡片显示 spinner（`.pixel-spinner`），数据回来后再填充
5. 按钮点击播放 `SFX.click()`

---

## 10. 验收自检

- [ ] 页面打开 → 显示 6 个卡片（3 状态 + 2 指标 + 1 安全）
- [ ] 系统状态卡实时反映 `/health` 状态
- [ ] DB 连接卡正确显示连接/断开
- [ ] 任务吞吐卡 4 行进度条 + 数值
- [ ] Agent 调用卡 5 行进度条 + 数值
- [ ] 安全事件日志卡 4 行阻断记录
- [ ] 每 10 秒自动刷新
- [ ] 离开页面后定时器停止（unmount 清理）
- [ ] Prometheus 文本解析不抛异常（容错）
- [ ] 首次加载有 spinner
- [ ] 点 ← 仪表盘按钮回到 dashboard

---

## 11. 文件清单

| 文件 | 操作 | 预期大小 |
|------|------|---------|
| `src/pages/monitor.js` | **新建** | ~10KB |
| `src/css/pixel.css` | **新增** `.pixel-card-title` 类 | 已有基础上追加 |
| 其他所有文件 | **不修改** | — |

---

## 12. pixel.css 追加内容

在 `pixel.css` 末尾追加以下一个类：

```css
.pixel-card-title {
  font-family: var(--pixel-font);
  font-size: 10px;
  color: var(--pixel-light);
  letter-spacing: 2px;
  text-transform: uppercase;
  margin-bottom: 14px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--pixel-mid);
}
```

**不要改 pixel.css 中已有的任何行**——只在文件最末尾追加。
