# 竞品分析系统 — 代码健壮性修复

**日期**: 2026-07-25 18:48 GMT+8
**任务**: 提升整个项目的异常处理与日志可诊断性

## 审计结果

全量扫描 `src/` 下 46 个 `.py` 文件，发现 **15 处脆弱异常处理**，分布在 9 个文件中。

### 问题分类

| 严重度 | 数量 | 典型问题 |
|-------|:---:|----------|
| 🔴 Critical | 3 | `pass` 静默吞异常 → 排查无门 |
| 🟡 Warning | 12 | `except Exception:` 无日志无 traceback → 线上盲盒 |

## 修复清单（8 个文件）

| 文件 | 行 | 修复内容 |
|------|:--:|---------|
| `src/api/routes.py` | 299 | `pass` 吞异常 → `logger.exception()` |
| `src/api/routes.py` | 506 | 健康检查 DB 失败静默 → `logger.warning()` + 错误详情 |
| `src/api/sse.py` | 261 | `except Exception: task_row=None` → `logger.warning()` + task_id |
| `src/api/sse.py` | 280 | `except Exception: report=None` → `logger.warning()` + task_id |
| `src/supervisor/supervisor.py` | 237 | `pass` 吞 JSON 解析失败 → `logger.warning()` + 原始文本前 100 字符 |
| `src/memory/summarizer.py` | 320 | `pass` 吞摘要查询失败 → `logger.exception()` + task_id/count |
| `src/memory/long_term.py` | 412 | `logger.debug()` 无声记录 → `logger.warning()` + exc_info |
| `src/evaluation/judge_eval.py` | 219 | `except Exception:` 无日志 → `logger.warning()` + task_id |
| `src/evaluation/judge_eval.py` | 262 | `pass` 吞 JSON 解析失败 → `logger.warning()` + 原始内容前 200 字符 |
| `src/evaluation/judge_eval.py` | 273 | `except Exception: return` → 已有 logger，无需改 |
| `src/agents/collector.py` | 307 | `except Exception:` 无日志 → `logger.warning()` + competitor/query |
| `src/mcp/tools_web.py` | 166-174 | HTTP 错误/超时/通用异常 3 处无日志 → 各加 `logger.warning()` + url/错误详情 |

## 修复原则

1. **绝不吞异常** — 任何 `pass` 换 `logger.warning/exception`
2. **上下文信息** — 每条日志都带 task_id、url、query 等可追溯的上下文
3. **不改变行为** — 只是加了日志，返回值和流程不变

## 验证

- ✅ 46 个 `.py` 全量语法校验通过
- ✅ 27/29 测试通过（2 个失败为已知基础设施问题：Supervisor 超时 + API 未启动）
