# Pipeline 三 Bug 修复与日志增强 — 2026-07-25

## 背景
首次端到端运行竞品分析系统，日志中出现三个 error 和一个静默行为。

## 修复

### Bug 1：LLM 返回空内容导致 `JSONDecodeError`
- **现象**：`resp.content` 为 `None`，`.strip()` 直接 `AttributeError`
- **根因**：LLM 偶尔返回空（rate limit / 上下文溢出），没有防御
- **修复**（`collector.py: _generate_keywords()`）：
  - `text = (resp.content or "").strip()` → 空值防御
  - 空内容直接 `raise ValueError`，走模板兜底
  - 非 `[` 开头的文本自动从中间抠出 JSON 部分（`text.find("[")`）
  - `text.split("``")` 防御嵌套反引号（文档已有 ```` ``` ```` 标记）

### Bug 2：百度百科 403 Forbidden
- **现象**：`httpx` 抓取 `baike.baidu.com` 返回 403
- **根因**：百度百科双重反爬（TLS fingerprint + cookie 验证），纯 HTTP 请求无法通过
- **修复**（`collector.py` 搜索结果去重阶段）：
  - 增加 `_ANTI_SCRAPE_DOMAINS = {"baike.baidu.com"}` 黑名单
  - 去重时跳过黑名单域名，不发起抓取请求
  - 用搜索引擎返回的 `title` + `snippet` 作为兜底摘要
  - `skip_baike` 计数器 + INFO 日志
- **附带**（`tools_web.py: web_fetch()`）：
  - 百度系域名加 `Referer: https://www.baidu.com/` 头
  - 其他百度子域名（非百科）可能受益

### Bug 3：`XLMRobertaTokenizer.prepare_for_model` 不存在
- **现象**：`rerank failed` 两次，stack 指向 `prepare_for_model`
- **根因**：`FlagEmbedding 1.4.0` + `transformers 5.12.1` 不兼容  
  `transformers 5.x` 移除了 `tokenizer.prepare_for_model()` 方法
- **修复**（`tools_rag.py: _get_reranker_model()` + `rerank()`）：
  - `FlagEmbedding.FlagReranker` → `sentence_transformers.CrossEncoder`
  - `CrossEncoder` 是 HuggingFace 官方 cross-encoder 接口，内部做了兼容层
  - `model.compute_score(pairs, normalize=True)` → `model.predict(pairs)` + 手动 sigmoid
  - 已实测验证：`CrossEncoder('BAAI/bge-reranker-v2-m3')` 正常工作

### 修复 4：Pipeline 中间节点日志静默（日志配置）
- **现象**：只有 `【Pipeline】剩余步数耗尽 (steps=0)` 一条，看不到 collect/analyze/write/quality 的执行日志
- **根因**：Python 默认 `logging` WARNING，`logger.info()` 全部被过滤
- **修复**（`main.py`）：`logging.basicConfig(level=logging.INFO, ...)`

## 修改文件
| 文件 | 修改内容 |
|------|----------|
| `src/agents/collector.py` | 空响应防御 + JSON 提取容错 + 反爬域名黑名单 |
| `src/mcp/tools_rag.py` | FlagReranker → CrossEncoder（修复 transformers 5.x 兼容性） |
| `src/mcp/tools_web.py` | 百度系域名加 Referer 头 |
| `src/main.py` | 全局日志级别 → INFO |

## 验收
- 3 个 error 不再出现 ✅
- 无新语法错误（`py_compile` 全部通过） ✅
- CrossEncoder 实测正常 ✅
- 日志框架调通 ✅
