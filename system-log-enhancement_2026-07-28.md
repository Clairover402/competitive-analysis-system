# System Log 前端展示增强 — 2026-07-28

## 问题

前端 SSE 进度展示的 system log 信息太少，每次只显示"正在采集竞品数据..."这类静态短消息，完全没有体现控制台日志里的详细信息（页面数、片段数、维度名、评分明细、耗时等）。

## 根因

1. **`_format_progress_message()`** 接收了 `response` 参数但完全忽略它，只用 `agent`+`action` 查静态映射表返回短消息
2. **`event_generator()`** 没有把 `duration_ms` 传给格式化函数，也没有放入 SSE 事件
3. **各 Agent 的 `log_dao.log()` 调用** 的 `response` 字段只有最少信息，缺 per-competitor/per-dimension/dimension_scores 等细节

## 改了什么（4个文件）

### 1. `src/api/sse.py` — 核心改动
- `event_generator()`: 提取 `duration_ms`，传给 `_format_progress_message()`，并放入所有 SSE progress 事件
- `_format_progress_message()`: **完全重写**，从静态映射表改为基于 response 数据的动态消息生成：
  - **Collector**: `✅ Collector 采集完成: 3竞品 → 飞书(5页/12段)、钉钉(4页/9段)、企微(3页/8段) · 3.2秒`
  - **Analyzer**: `✅ Analyzer 分析完成: 5维度 → 定价(3竞品)、功能(3竞品)、安全性(2竞品) · 4.5秒`
  - **Writer**: `✅ Writer 报告生成: 3,200字符 · 1.2秒`（改写版前缀 `🔄`）
  - **Quality**: `✅ Quality 质检评分: 82.5分 ✅通过 | 完整性=85分 | 准确性=80分 | ... · 0.8秒`
  - **Supervisor**: 预留路由决策格式
  - 所有消息末尾追加耗时

### 2. `src/agents/collector.py`
- `log_dao.log()` 的 response 新增: `competitors_count`, `per_competitor`（每个竞品的 pages/chunks/urls）

### 3. `src/agents/analyzer.py`
- `log_dao.log()` 的 response 新增: `dimensions_count`, `dimension_names`, `per_dimension`（每个维度的 competitors_analyzed/has_data）
- 顺便修复第345行预存 bug：f-string 内 `result["error"]` 引号冲突 → `result['error']`

### 4. `src/agents/writer.py`
- `log_dao.log()` 的 response 新增: `rewrite` 布尔标记

### 5. `src/agents/quality.py`
- `log_dao.log()` 的 response 新增: `dimension_scores`（各维度评分明细）

## 效果对比

| 改进前 | 改进后 |
|--------|--------|
| `正在采集竞品数据...` | `✅ Collector 采集完成: 3竞品 → 飞书(5页/12段)、钉钉(4页/9段)、企微(3页/8段) · 3.2秒` |
| `正在多维度分析竞品...` | `✅ Analyzer 分析完成: 5维度 → 定价(3竞品)、功能(3竞品)、安全性(2竞品) · 4.5秒` |
| `正在生成分析报告...` | `✅ Writer 报告生成: 3,200字符 · 1.2秒` |
| `正在质检报告...` | `✅ Quality 质检评分: 82.5分 ✅通过 \| 完整性=85分 \| 准确性=80分 \| 可追溯性=75分 · 0.8秒` |

## 设计决策

- response 结构保持为 `dict`（JSONB 可序列化），不在 SSE 层拼大段文本——格式在 `_format_progress_message` 集中处理
- per_competitor/per_dimension 是字典，方便前端未来扩展 Timeline 展开/收起子列表
- 耗时以 `· X秒` 追加到每条消息末尾，与控制台日志风格一致
- 所有编辑通过 `py_compile` 验证
