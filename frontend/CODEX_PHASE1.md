# Phase 1 — 前端基础框架 + 登录 + 仪表盘（像素风）

> **Codex Prompt · 2026-07-27**
> 先读 `FRONTEND_OVERVIEW.md` 了解项目架构
> 执行目录：`D:\AAAagent\projects\competitive-analysis-ui\`

---

## 🎨 设计方向：精致的像素终端

### 风格参考

**不是** NES 那种 4 色瓷砖块。参考这些：

- **PICO-8** — 16 色调色板，柔和不刺眼，暗夜调居多
- **Celeste / Dead Cells 菜单** — 像素字体 + 暗底 + 单色高亮，干净克制
- **Hyper Light Drifter UI** — 深底色 + 细像素边框 + 霓虹点缀，科幻感
- **Loop Hero 对话框** — 粗像素边框但配色统一，不花哨

### 核心原则

```
✅ 暗色基底 + 一个主色调统一全局（绿色系或琥珀色系，像 CRT 终端）
✅ Press Start 2P 作为标题字体完全可以，但要控制使用范围（标题/按钮/标签，正文不用）
✅ 像素边框用 2px-4px box-shadow 三段式模拟，这是像素风的核心识别符号
✅ 按钮有按压下沉效果（那个 2px 偏移是像素游戏 UI 的灵魂）
✅ 少量闪烁动画（光标/loading）是风格必需品，不是噪音
✅ 所有圆角为 0
✅ 色彩从 16 色调色板中选取，不随意使用 hex 颜色
✅ 按钮/输入框/面板的边框样式统一（box-shadow 三段式模拟像素边框）
```

### 禁止

```
❌ 1px 细边框 + border-radius 圆角 → 这不是像素风
❌ 系统 UI 字体（Segoe UI / -apple-system）大面积使用 → 像素风就得用像素字体或等宽
❌ 蓝色系主题 → 用绿色系或琥珀色系，像真实的 CRT 终端
❌ 背景纯黑 #000 → 用深灰绿 #0a0a0a 或更深
```

---

## 🎨 调色板（16 色 PICO-8 风格）

```css
:root {
  /* 背景系 */
  --pixel-black:      #0a0a0a;   /* 最暗背景 */
  --pixel-dark:       #1a1c2c;   /* 面板背景 */
  --pixel-mid:        #333c57;   /* 边框/分隔 */
  --pixel-light:      #5a6988;   /* 次要文字 */
  
  /* 前景系 */
  --pixel-white:      #e0e8f0;   /* 主文字 */
  --pixel-cream:      #fff4d2;   /* 暖白（标题/强调） */
  
  /* 主色调 — 绿色系（CRT 终端味） */
  --pixel-green:      #38b764;   /* 主色（按钮/边框/强调） */
  --pixel-green-dim:  #265c42;   /* 暗绿（hover 背景） */
  --pixel-green-dark: #19332f;   /* 深绿（终端背景） */
  
  /* 语义色 */
  --pixel-red:        #e43b44;   /* 错误/危险 */
  --pixel-yellow:     #f6c863;   /* 警告/进行中 */
  --pixel-blue:       #3b8eff;   /* 链接/信息 */
  --pixel-cyan:       #63c8f6;   /* 装饰点缀 */
  --pixel-purple:     #8968ba;   /* 特殊标记 */
  --pixel-orange:     #f0824b;   /* 强调/高亮 */
  
  /* 字体 */
  --pixel-font:       'Press Start 2P', monospace;
  --pixel-mono:       'Courier New', 'SimHei', monospace;   /* 终端/代码 */
  
  /* 像素边框规格 */
  --pixel-border:     4px;        /* box-shadow 三段式边框宽度 */
  --pixel-btn-depth:  4px;        /* 按钮按压下沉距离 */
}
```

---

## 1. pixel.css — 像素风 CSS 基础库

### 1.1 核心：三段式像素边框

像素风 UI 的灵魂是 box-shadow 模拟的粗边框。标准公式：

```css
/* 标准面板：三层 box-shadow = 4px 粗像素边框 */
.pixel-box {
  border: none;
  box-shadow:
    /* 第 1 层：内发光 */  inset 0 0 0 2px rgba(255,255,255,0.03),
    /* 第 2 层：深色外边框 */ 0 0 0 2px var(--pixel-black),
    /* 第 3 层：亮色外边框 */ 0 0 0 4px var(--pixel-green);
  background: var(--pixel-dark);
}
```

### 1.2 全局重置

```css
*, *::before, *::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

body {
  background: var(--pixel-black);
  color: var(--pixel-white);
  font-family: var(--pixel-mono);
  font-size: 14px;
  line-height: 1.7;
  min-height: 100vh;
  image-rendering: pixelated;
}
```

### 1.3 组件类

| 类名 | 用途 | 关键视觉 |
|------|------|---------|
| `.pixel-box` | 通用面板 | 三段式 box-shadow 像素边框 + `pixel-dark` 背景 + 24px padding |
| `.pixel-card` | 可悬停卡片 | 同 `.pixel-box` + `cursor:pointer` + hover 时边框色变亮 |
| `.pixel-btn` | 主按钮 | `pixel-green` 底色 + 右侧/底部 4px 深色阴影（按压时下沉 4px） |
| `.pixel-btn-secondary` | 次要按钮 | 透明底 + 像素边框 + hover 时填充暗色 |
| `.pixel-btn-danger` | 危险按钮 | `pixel-red` 底色 |
| `.pixel-input` | 输入框 | `pixel-black` 底色 + 像素边框 + 聚焦时边框变亮绿 |
| `.pixel-input-label` | 输入框标签 | 小号像素字体 + `pixel-cream` 色 |
| `.pixel-badge` | 状态标签 | 像素边框 + 前景色文字 |
| `.pixel-progress` | 进度条 | 像素边框容器 + 内部条纹填充 |
| `.pixel-tabs` | Tab 切换 | flex 容器 |
| `.pixel-tab` | Tab 按钮 | 激活时底部像素边框高亮 |
| `.pixel-divider` | 分隔线 | 2px 实线 `pixel-mid` + 两侧留白 |

### 1.4 像素按钮按压效果

```css
.pixel-btn {
  position: relative;
  border: none;
  box-shadow:
    /* 右侧阴影 */  4px 0 0 0 #1a6030,
    /* 底部阴影 */  0 4px 0 0 #1a6030;
  transition: all 0.05s steps(2);
  font-family: var(--pixel-font);
  font-size: 8px;
  letter-spacing: 1px;
  text-transform: uppercase;
}

/* 按压：按钮下沉 4px，阴影同时缩到 0 */
.pixel-btn:active {
  transform: translate(4px, 4px);
  box-shadow: 0 0 0 0 transparent;
}
```

### 1.5 光标闪烁

```css
@keyframes pixel-blink {
  0%, 49% { opacity: 1; }
  50%, 100% { opacity: 0; }
}
```

### 1.6 扫描线（可选，默认关闭）

```css
body.scanlines::after {
  content: '';
  position: fixed;
  inset: 0;
  background: repeating-linear-gradient(
    0deg,
    transparent,
    transparent 2px,
    rgba(0,0,0,0.03) 2px,
    rgba(0,0,0,0.03) 4px
  );
  pointer-events: none;
  z-index: 9999;
}
```

### 1.7 工具类

`.flex` `.flex-col` `.flex-wrap` `.items-center` `.justify-center` `.justify-between`
`.flex-1` `.gap-1` `.gap-2` `.w-full` `.hidden` `.relative`
`.text-center` `.text-right` `.text-green` `.text-red` `.text-yellow` `.text-cyan` `.text-dim`
`.mt-1` `.mt-2` `.mt-3` `.mb-1` `.mb-2` `.mb-3`
`.pixel-cursor`（绿色方块 + 闪烁动画，菜单项左侧指示器）

---

## 2. sounds.js — 8-bit 音效引擎

同现有架构（Web Audio OscillatorNode 方波），导出：

```js
export const SFX = {
  blip(),      // 短促轻音 — 菜单切换
  confirm(),   // 上行双音 — 确认操作
  error(),     // 下行双音 — 错误
  startup(),   // 五音琶音 — 登录成功
  click(),     // 极短 click — 按钮反馈
};
```

**要求：** 音量统一 ≤0.08，AudioContext 延迟初始化，不支持 Web Audio 时静默降级不报错。

---

## 3. api.js — API 封装

```js
export const API = {
  get(path),           // GET → JSON，自动注 JWT
  post(path, body),    // POST → JSON，自动注 JWT
  sse(path, handlers), // EventSource，token 走 query string
};
```

BASE_URL 为空（Vite proxy 转发到 :8000）。
非 2xx 响应抛 Error，消息取 `res.detail`。

---

## 4. auth.js — 鉴权

```js
export const Auth = {
  isLoggedIn(),   // token 存在且未过期
  getUsername(),  // 从 localStorage 取
  getUserId(),    // 从 localStorage 取
  guard(),        // 未登录 → 跳 #/login + return false
  logout(),       // 清空 localStorage + 跳 #/login
};
```

---

## 5. router.js — Hash 路由引擎

| Hash | 页面模块 | 需鉴权 | 参数 |
|------|---------|:--:|------|
| `#/login` | `login.js` | 否 | — |
| `#/dashboard` | `dashboard.js` | 是 | — |
| `#/task/:id` | `task-detail.js` | 是 | `id` |
| `#/report/:taskId/:reportId` | `report.js` | 是 | `taskId`, `reportId` |
| `#/monitor` | `monitor.js` | 是 | — |

未匹配 → 跳 `#/dashboard`。已登录访问 `#/login` → 跳仪表盘。
页面模块动态 `import()`。模块契约：`{ render(params), mount(params), unmount() }`。
task-detail / report / monitor 不存在时优雅降级。

---

## 6. login.js — 登录/注册页

### 视觉布局

```
┌──────────────────────────────────────────────────────────┐
│                                                          │
│                    [🔍 大 Logo 图标]                      │
│                                                          │
│              竞 品 分 析 系 统  v1.0                       │  ← Press Start 2P, 11px, pixel-green
│                    命令行终端风格居中卡片                    │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │                                                    │  │
│  │  USERNAME:                                         │  │  ← pixel-input-label
│  │  ╔══════════════════════════════════════════╗     │  │
│  │  ║ 输入用户名 ...                             ║     │  │  ← 像素边框输入框
│  │  ╚══════════════════════════════════════════╝     │  │
│  │                                                    │  │
│  │  PASSWORD:                                         │  │
│  │  ╔══════════════════════════════════════════╗     │  │
│  │  ║ ••••••••                                  ║     │  │
│  │  ╚══════════════════════════════════════════╝     │  │
│  │                                                    │  │
│  │  ┌──────────────────┐ ┌──────────────────┐       │  │
│  │  │   LOGIN           │ │   REGISTER       │       │  │  ← 像素按钮
│  │  └──────────────────┘ └──────────────────┘       │  │
│  │                                                    │  │
│  │  > 登录中...                                       │  │  ← 终端风格消息
│  │                                                    │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│              Press ENTER to login                         │  ← pixel-mono, 小号, 灰色
│                                                          │
└──────────────────────────────────────────────────────────┘
```

### 视觉规格

- 页面背景：`pixel-black`，无渐变
- 卡片：`.pixel-box`，宽度 420px，居中
- Logo 图标使用 Unicode 大号符号（32px+），绿色
- 标题"竞品分析系统"：`pixel-font` 11px，`pixel-green`，letter-spacing 3px
- 输入框标签：`pixel-font` 7px，`pixel-cream`，全大写英文
- 输入框：`.pixel-input`，placeholder 小号像素字体
- 按钮并排，等宽，用 `pixel-font` 8px 大写
- 消息区：`pixel-mono` 12px，`>` 前缀终端风格，成功绿色/错误红色
- 底部提示："Press ENTER to login" 小号灰色 `pixel-mono`

### 交互逻辑

- 用户名输入框自动聚焦
- Enter 在用户名框 → 跳到密码框
- Enter 在密码框 → 执行登录
- 登录成功 → `SFX.startup()` → 0.6s 后跳 `#/dashboard`
- 注册成功 → `SFX.confirm()` → 0.8s 后跳 `#/dashboard`
- 注册前弹 `confirm()` 确认
- 错误消息 5 秒后淡出

---

## 7. dashboard.js — 仪表盘

### 整体布局

```
┌─────────────┬──────────────────────────────────────────────────┐
│             │                                                  │
│  侧边栏     │                主内容区                           │
│  ═════════  │                                                  │
│  🔍 竞品    │  ╔══════════════════════════════════════╗      │
│  分析       │  ║  [ FAST MODE ] [ ADVANCED ]          ║      │  ← pixel-tabs
│             │  ╠══════════════════════════════════════╣      │
│  ■ 仪表盘   │  ║                                      ║      │
│  □ 监控面板 │  ║  > 输入分析描述 ...                   ║      │
│             │  ║                                      ║      │
│             │  ║  ┌──────────────────────────────┐   ║      │
│             │  ║  │   ▶ EXECUTE                  │   ║      │
│             │  ║  └──────────────────────────────┘   ║      │
│             │  ╚══════════════════════════════════════╝      │
│ ═══════════ │                                                  │
│             │  ┌─ ▸ TASK QUEUE ── [REFRESH] ──────────────┐  │
│  👤 用户名  │  │                                            │  │
│ [SIGNOUT]   │  │  ┌──────┐  ┌──────┐  ┌──────┐           │  │
│             │  │  │任务1  │  │任务2  │  │任务3  │           │  │
│             │  │  └──────┘  └──────┘  └──────┘           │  │
│             │  └────────────────────────────────────────────┘  │
└─────────────┴──────────────────────────────────────────────────┘
```

### 7.1 左侧边栏

- 宽度 200px，`pixel-dark` 背景，右侧像素分隔线
- 顶部 Logo 区：大图标 + "竞品分析"（`pixel-font` 8px，green）
- 菜单项：每项有 `.pixel-cursor`（选中时显示绿色方块），选中项高亮 `pixel-green-dim`
- 底部：用户名 + `pixel-btn-secondary` "SIGNOUT"

### 7.2 创建任务区

两模式切换，Tab 用底部高亮条（2px `pixel-green`）。

**快速模式（FAST MODE）：**
- 3 行 textarea，`.pixel-input` 样式
- placeholder："对比 iPhone 16、华为 Mate 70 和小米 15 在价格、性能、摄像头方面的表现"
- 下方 `pixel-btn` "▶ EXECUTE"

**高级模式（ADVANCED）：**
- 三个 `.pixel-input`：标题 / 竞品（逗号分隔）/ 维度（逗号分隔）
- 下方 `pixel-btn` "▶ EXECUTE"

**提交逻辑：**
- 快速：POST `/api/tasks` `{ title: query[0:80], query }`
- 高级：POST `/api/tasks` `{ title, competitors, dimensions }`
- 提交成功 → `SFX.confirm()` → 将 task_id 写入 localStorage `_task_ids` 列表 → 跳 `#/task/{id}`
- 提交失败 → `SFX.error()` → 显示红色错误消息

### 7.3 任务列表

- 标题 "▸ TASK QUEUE" + 右侧 [REFRESH] 按钮
- 从 localStorage `_task_ids` 取 ID，逐个 `GET /api/tasks/{id}`，取最近 20 条
- 卡片 3 列网格，`.pixel-card`
- 每卡：状态 badge + 标题（单行截断）+ 时间 + 竞品数
- 状态映射：pending/collecting/analyzing/writing/qa → yellow badge，completed → green，failed → red
- 空态：`pixel-mono` 灰色文字 "NO TASKS IN QUEUE" + 提示文字

---

## 8. 交互细节

1. 页面切换时新内容 `pixel-box` 有 `fadeIn` 动画（0.2s）
2. 所有按钮点击：`SFX.click()` + `scale(0.98)` 或像素按压下沉
3. Enter 键提交（textarea 用 Ctrl+Enter）
4. 错误消息 5 秒后自动淡出
5. 输入框默认自动聚焦
6. 提交中按钮显示 "..." 且 disabled

---

## 9. 验收清单

- [ ] 打开项目 → 看到绿色像素风登录页
- [ ] 输入框有像素边框、Press Start 2P 标签
- [ ] 按钮有 4px 阴影，按下有下沉效果
- [ ] 登录 → 仪表盘，侧边栏有 `■` 选中指示器
- [ ] 创建任务 → 跳 task-detail（若未实现则优雅降级）
- [ ] 任务列表卡片有像素边框 + hover 高亮
- [ ] 退出登录生效
- [ ] 刷新页面登录态保持
- [ ] 控制台无红色报错
- [ ] **有像素感，但不廉价**
