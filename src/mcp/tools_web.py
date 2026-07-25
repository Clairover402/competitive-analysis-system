"""MCP Web 工具 — 互联网搜索与网页抓取。
============================================================

【L3 架构定位】
web_search / web_fetch 是 Collector Agent 的两只"手"。
在竞品分析流程中：
  web_search("飞书定价 2025")  → 获取 URL 列表 + 摘要
  web_fetch(url) 每个 URL     → 提取正文 → 存入 chunk_embeddings
  embed_texts(chunks)            → 向量化 → similarity_search 检索

整个采集管线 = search → fetch → chunk → embed → index。

【L4 工程考量】为什么选 cn.bing.com 不用 DuckDuckGo？
------------------------------------------------------------
1) 国内直连：cn.bing.com 在国内无需代理，零额外配置
2) 零 API Key：HTML 抓取 + 正则解析，不需要注册、不需要配额管理
3) 结果质量：Bing 中文搜索结果优于 DuckDuckGo
4) 稳定性：微软中国站 CNB 解析规则稳定

缺点：HTML 结构可能变化导致正则失效。生产级方案是 Playwright MCP。

【L5 项目对标】
这两个工具通过 MCPServer 注册后，Collector Agent 通过
tools/list 发现它们，用 tools/call 调用它们——
Agent 不知道工具内部实现，只知道 schema。
"""

from __future__ import annotations

import html
import logging
import re

import httpx

from src.config import Settings

logger = logging.getLogger(__name__)

# ============================================================
# 浏览器 User-Agent —— 模拟真实浏览器，避免被反爬
# Chrome 125 on Windows 10 —— 2025年市场占有率最高的浏览器配置
# 不加 UA 或加 requests 默认 UA (python-requests/2.x) 会被很多站直接拒
# ============================================================
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


async def web_search(
    query: str,
    max_results: int = 10,
    settings: Settings | None = None,
) -> list[dict]:
    """搜索互联网获取信息——DuckDuckGo HTML 版。

    【L4 工程】超时设计：为什么是 15s？
    ------------------------------------------------------------
    P99 延迟经验值：正常搜索 < 3s，网络波动 < 8s。
    15s 是"宁可超时也不让 Agent 卡死"的阈值。
    竞品分析一次要搜十几个 query——如果一个卡 30s，整个任务拖到分钟级。

    【L4 降级策略】
    搜索失败 → 返回空列表 [] → 上层 Agent 判断：
      - 如果 result 为空：标记该维度"数据不可用"，用已有信息继续
      - 如果 3 次重试仍空：跳过该维度，在报告中注明信息来源受限
    不抛异常——让分析流程能容错推进。

    Args:
        query: 搜索关键词。
        max_results: 最大返回数量（默认 10）。
        settings: 配置实例。

    Returns:
        [{title: 标题, url: 链接, snippet: 摘要}, ...]，
        失败时返回空列表，不抛异常。
    """
    try:
        # httpx.AsyncClient 作为上下文管理，自动关闭连接
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://cn.bing.com/search",
                params={"q": query, "ensearch": "0"},
                headers={
                    "User-Agent": _BROWSER_UA,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            resp.raise_for_status()  # 4xx/5xx → HTTPStatusError
            results = _parse_bing(resp.text, max_results)
            logger.info("web_search: query=%r results=%d", query, len(results))
            return results
    except Exception:
        # 【L4 工程】不区分异常类型，全部兜底返回空。
        # 搜索是辅助能力，失败不应阻断主流程。
        logger.exception("web_search failed for query=%r", query)
        return []


async def web_fetch(
    url: str,
    max_chars: int = 10000,
    settings: Settings | None = None,
) -> dict:
    """抓取网页内容并提取正文文本。

    【L4 工程】为什么用正则提取文本不用 BeautifulSoup？
    ------------------------------------------------------------
    1) 减少依赖：BeautifulSoup + lxml 加起来 ~15MB，正则 0 依赖
    2) 速度：正则比 BS4 快 3-5 倍（不需要构建 DOM 树）
    3) 够用：竞品分析场景只需提取正文文字，不需要 CSS 选择器定位
    缺点：嵌套标签、动态渲染（SPA）页面提取效果差。
          这是 tradeoff——简洁性 vs 覆盖率。

    【L4 工程】超时 20s 为什么比 search 多 5s？
    ------------------------------------------------------------
    搜索只需要 DuckDuckGo 响应（CDN 加速，快），
    但 web_fetch 的目标 URL 可能是任意网站——
    有的站服务器慢、有的大页面 > 5MB、有的在海外。
    多 5s 缓冲应对这些情况。

    Args:
        url: 目标网页 URL。
        max_chars: 最大提取字符数（默认 10000，约 10KB 文本）。
        settings: 配置实例。

    Returns:
        {url, title, text_content, status_code, error}
        失败时 text_content=""，error 字段包含原因。
    """
    result: dict = {
        "url": url,
        "title": "",
        "text_content": "",
        "status_code": 0,
        "error": "",
    }

    try:
        # 【L4 工程】反反爬策略——不同域名使用不同的请求头
        # - 百度百科 (baike.baidu.com)：要求 Referer 否则 403
        # - 知乎/CSDN 等：需要 Accept-Language 否则可能返回移动端乱码
        headers = {
            "User-Agent": _BROWSER_UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        # 百度系域名必须带 Referer，否则直接拒绝访问（403 Forbidden）
        if "baidu.com" in url or "baike.baidu.com" in url:
            headers["Referer"] = "https://www.baidu.com/"

        async with httpx.AsyncClient(
            timeout=20.0,
            headers=headers,
            follow_redirects=True,   # 自动跟随重定向（301/302）
        ) as client:
            resp = await client.get(url)
            result["status_code"] = resp.status_code
            resp.raise_for_status()

            html = resp.text

            # 提取 <title> 标签
            title_match = re.search(
                r"<title[^>]*>(.*?)</title>", html, re.I | re.S
            )
            if title_match:
                result["title"] = _clean_html(title_match.group(1)).strip()

            # 提取正文：去 script/style/noscript/iframe 后取纯文本
            text = _extract_text(html)
            result["text_content"] = text[:max_chars]

    except httpx.HTTPStatusError as e:
        # HTTP 错误（4xx/5xx）—— 返回状态码 + 描述
        logger.warning("HTTP 错误 url=%s status=%s", url, e.response.status_code)
        result["error"] = f"HTTP {e.response.status_code}"
    except httpx.TimeoutException:
        # 超时 —— 单独标记，方便上游决策是否重试
        logger.warning("请求超时 url=%s", url)
        result["error"] = "timeout"
    except Exception as e:
        # 其他异常（DNS 解析失败、连接重置等）
        logger.warning("请求失败 url=%s error=%s", url, e)
        result["error"] = str(e)

    return result


# ============================================================
# HTML 解析辅助函数
# 选正则不用 BS4 —— 原因见上方 docstring 的【L4 工程】注释
# ============================================================

def _parse_bing(html: str, max_results: int) -> list[dict]:
    """从 cn.bing.com 搜索结果页提取搜索结果。

    Bing 搜索结果结构（2025年7月）：
    每条结果包裹在 class="b_algo" 的 li 标签内，
    内含 <h2><a href="...">标题</a></h2> + <p class="b_lineclamp2">摘要</p>。

    ⚠️ Bing 改版时此解析可能失效——届时需更新正则。
    """
    results: list[dict] = []
    blocks = re.split(r'class="b_algo"', html)[1:]
    for block in blocks[:max_results]:
        # 提取标题链接 <h2>...<a href="URL">TITLE</a>...</h2>
        h2_block = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        # 提取摘要 <p class="b_lineclamp2">SNIPPET</p>
        snippet_m = re.search(r'class="b_lineclamp\d*"[^>]*>(.*?)</p>', block, re.S)

        url = h2_block.group(1) if h2_block else ""
        title = _clean_html(h2_block.group(2)) if h2_block else ""
        snippet = _clean_html(snippet_m.group(1)) if snippet_m else ""

        # 至少有标题或 URL 才收录
        if title or url:
            results.append({
                "title": title.strip(),
                "url": url.strip(),
                "snippet": snippet.strip(),
            })

    return results


def _clean_html(text: str) -> str:
    """去除 HTML 标签并解码常见实体。

    HTML 实体 → 原字符：
      &amp;  → &
      &lt;   → <
      &gt;   → >
      &quot; → "
      &#x27; → '
    """
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&#x27;", "'")
    text = html.unescape(text)  # 解码 &#x738B; → 王 等 Unicode 实体
    return re.sub(r"\s+", " ", text)


def _extract_text(html: str) -> str:
    """从 HTML 中提取纯文本正文。

    四步处理：
    1) 移除 script/style/noscript/iframe 标签（含内容）
    2) 移除 HTML 注释 <!-- ... -->
    3) 剩余标签 → 纯文本（去标签保留文字）
    4) 压缩连续空行为最多 2 行
    """
    # 移除不需要的标签 + 内容
    for tag in ("script", "style", "noscript", "iframe"):
        html = re.sub(
            f"<{tag}[^>]*>.*?</{tag}>", " ", html, flags=re.I | re.S
        )
    # 移除 HTML 注释
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    # 剩余 HTML → 纯文本
    text = _clean_html(html)
    # 压缩多余空白行
    return re.sub(r"\n{3,}", "\n\n", text).strip()
