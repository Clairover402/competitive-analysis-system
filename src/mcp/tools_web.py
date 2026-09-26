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
    """搜索互联网获取信息——三层引擎 fallback（Bing → Sogou → synthesize）。

    【L4 工程】三层 fallback 架构
    ------------------------------------------------------------
    竞品分析系统对"零搜索结果"极其敏感——搜索挂了 → 采集无数据 →
    分析全是[数据不足] → 报告低分 → 改写循环白耗 token。
    所以搜索不能只有一个引擎：

    L1: Bing (cn.bing.com)      主引擎，最快最稳定
    L2: Sogou (search.sogou.com) 备用引擎，国内直连
    L3: synthesize_urls()        终极兜底——构造高质量定向 URL
         (site:zhihu.com + 关键词) 保证至少返回几个候选

    三层设计保证：Bing 正则失效/Sogou 改版/synthesize 全部同时挂 →
    概率接近零。三层只要有一层活着，pipeline 就有数据可跑。

    【L4 工程】超时设计：为什么是 15s？
    ------------------------------------------------------------
    P99 延迟经验值：正常搜索 < 3s，网络波动 < 8s。
    15s 是"宁可超时也不让 Agent 卡死"的阈值。
    竞品分析一次要搜十几个 query——如果一个卡 30s，整个任务拖到分钟级。

    Args:
        query: 搜索关键词。
        max_results: 最大返回数量（默认 10）。
        settings: 配置实例。

    Returns:
        [{title: 标题, url: 链接, snippet: 摘要}, ...]，
        极端情况下返回 synthesize 构造的 URL 列表，绝不返回空。
    """
    # ── L0: Tavily 主引擎（AI Agent 专用，配置了 key 才启用）──
    # 【2026-09-26】cn.bing.com 对游戏/产品竞品 query 做了强 SEO 干预，
    # 前 10 条全是官网/下载页/应用商店/百科，点评文章一条都搜不到。
    # Tavily 返回的本身就是网页正文（不只是链接），且支持 include_domains/
    # exclude_domains 过滤官网，是“搜到点评文章”的最直接方案。
    #
    # 【关键】include_raw_content=True：让 Tavily 服务端渲染并把正文塞进结果，
    # 解决“搜到 URL → httpx 抓不到 SPA 正文”的死结（jina.ai 实测不可达）。
    if settings.tavily_enabled and settings.tavily_api_key:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": settings.tavily_api_key,
                        "query": query,
                        "max_results": settings.tavily_max_results or max_results,
                        "search_depth": "basic",   # basic=1 credit，advanced=2 credits
                        "include_raw_content": True,  # 服务端渲染正文，绕开 SPA
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                raw_results = data.get("results", [])
                results = [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "snippet": r.get("content", "") or r.get("description", ""),
                        "raw_content": r.get("raw_content", ""),  # 服务端渲染正文
                    }
                    for r in raw_results[:max_results]
                    if r.get("url")
                ]
                if results:
                    logger.info(
                        "web_search[Tavily]: query=%r results=%d", query, len(results)
                    )
                    return results
                else:
                    logger.warning(
                        "web_search[Tavily]: query=%r 返回0结果，切换 Bing", query
                    )
        except Exception:
            logger.warning(
                "web_search[Tavily] 失败 query=%r，切换 Bing", query, exc_info=True
            )

    # ── L1: Bing 主引擎 ──
    try:
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
            if results:  # 解析成功且有结果
                logger.info("web_search[Bing]: query=%r results=%d", query, len(results))
                return results
            else:
                logger.warning("web_search[Bing]: query=%r 返回0结果，切换 Sogou", query)
    except Exception:
        logger.warning("web_search[Bing] 失败 query=%r，切换 Sogou", query, exc_info=True)

    # ── L2: Sogou 备用引擎 ──
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://search.sogou.com/web",
                params={"query": query},
                headers={
                    "User-Agent": _BROWSER_UA,
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            resp.raise_for_status()
            results = _parse_sogou(resp.text, max_results)
            if results:
                logger.info("web_search[Sogou]: query=%r results=%d", query, len(results))
                return results
            else:
                logger.warning("web_search[Sogou]: query=%r 返回0结果，使用 synthesize 兜底", query)
    except Exception:
        logger.warning("web_search[Sogou] 失败 query=%r，使用 synthesize 兜底", query, exc_info=True)

    # ── L3: synthesize 终极兜底 ──
    # 构造定向高质量 URL：site:zhihu.com / site:csdn.net / site:36kr.com
    results = _synthesize_urls(query, max_results)
    logger.info("web_search[synthesize]: query=%r results=%d（构造URL）", query, len(results))
    return results


async def _jina_fetch(url: str, max_chars: int = 10000) -> dict:
    """Jina Reader 兜底抓取 —— 免费无 key，服务端渲染后再抓。

    【2026-09-26】为什么需要 Jina Reader？
    ------------------------------------------------------------
    现代官网/点评平台（pvp.qq.com、TapTap、B站专栏）是 SPA，
    正文靠 JS 动态加载，httpx 只能拿到几十个字符的骨架。
    Jina Reader（r.jina.ai）在服务端用无头浏览器渲染页面，
    再把正文转成干净的 Markdown 文本返回——等于“免费的 Playwright”。

    用法：https://r.jina.ai/{url}
    无 key、无注册，速率受限但对竞品分析的单页兜底足够。

    Returns:
        同 web_fetch 的结果 dict，失败时 error 字段非空。
    """
    result: dict = {
        "url": url,
        "title": "",
        "text_content": "",
        "status_code": 0,
        "error": "",
    }
    try:
        jina_url = f"https://r.jina.ai/{url}"
        async with httpx.AsyncClient(
            timeout=30.0,
            headers={
                "User-Agent": _BROWSER_UA,
                "Accept": "text/markdown, text/plain, */*",
            },
            follow_redirects=True,
        ) as client:
            resp = await client.get(jina_url)
            result["status_code"] = resp.status_code
            resp.raise_for_status()
            text = resp.text
            result["text_content"] = text[:max_chars]
            # 尝试从 Markdown 首行提取标题（Jina 返回 # 标题 开头）
            first_line = text.lstrip().split("\n", 1)[0] if text else ""
            if first_line.startswith("#"):
                result["title"] = first_line.lstrip("# ").strip()
    except httpx.HTTPStatusError as e:
        result["error"] = f"HTTP {e.response.status_code}"
    except httpx.TimeoutException:
        result["error"] = "timeout"
    except Exception as e:
        result["error"] = str(e)
    return result


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

            # ── 编码检测 ──
            # 【2026-07-29 修复】httpx 的 resp.text 用 response header 的 charset 解码，
            # 但很多中文网站（如 lol.qq.com、很多老站）header 写的是 UTF-8，
            # 实际内容是 GBK/GB2312 → 解码后全是乱码（"Ӣ������" 这类）。
            # 修复：先读原始字节 → 从 <meta charset> 标签检测真实编码 → 正确解码。
            raw_bytes = resp.content
            html = _decode_html(raw_bytes)

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

    # ── Jina Reader 兜底：正文过少（SPA 骨架）时重试 ──
    # 【2026-09-26】httpx 抓 SPA 页面只能拿到 JS 骨架，正文 CJK < 200。
    # 此时用 Jina Reader 服务端渲染再抓一次，能拿到真正文。
    if settings is not None and settings.jina_reader_enabled:
        cjk = sum(1 for ch in result["text_content"] if '\u4e00' <= ch <= '\u9fff')
        if cjk < 200:
            logger.info(
                "web_fetch: 正文过少(CJK=%d)，Jina Reader 兜底 url=%s", cjk, url
            )
            jina = await _jina_fetch(url, max_chars)
            # 只有 Jina 拿到的正文明显更多才替换，否则保留原结果
            jina_cjk = sum(
                1 for ch in jina["text_content"] if '\u4e00' <= ch <= '\u9fff'
            )
            if jina_cjk > cjk:
                result = jina

    return result


# ============================================================
# HTML 解析辅助函数
# 选正则不用 BS4 —— 原因见上方 docstring 的【L4 工程】注释
# ============================================================

# ── 常见中文编码列表（按优先级排序）──
_MANDARIN_ENCODINGS = ["utf-8", "gbk", "gb2312", "gb18030", "big5"]


def _decode_html(raw_bytes: bytes) -> str:
    """智能编码检测 → 正确解码 HTML 原始字节。

    【2026-07-29 修复】很多中文网站 HTTP response header 声称 charset=utf-8，
    但实际内容是 GBK/GB2312（如 lol.qq.com）。httpx 的 resp.text 直接按 header
    解码 → 中文乱码（"Ӣ������"）。

    修复策略（两步）：
      ① 先用 latin-1 解码前 8KB，正则找出 <meta charset="..."> 声明的真实编码
      ② 如果 meta 标签没声明，遍历中文字符密度打分（UTF-8 → GBK → GB2312 → GB18030）
      ③ 选出中文字符密度最高的解码结果——宁可多试几次，不输出乱码

    Args:
        raw_bytes: HTTP 响应的原始字节

    Returns:
        正确解码的 HTML 字符串
    """
    # ── 步骤①：从 <meta> 标签检测编码 ──
    # 用 latin-1 解码前 8KB（latin-1 是单字节编码，所有 256 个码点都可解码，不会报错）
    # 然后正则提取 charset 声明
    head_bytes = raw_bytes[:8192]
    try:
        head_text = head_bytes.decode("latin-1")
        # 匹配 <meta charset="gbk"> 或 <meta http-equiv="Content-Type" content="...charset=gb2312">
        charset_m = re.search(
            r'<meta[^>]+charset=["\']?([a-zA-Z0-9_\-]+)',
            head_text, re.I,
        )
        if charset_m:
            detected = charset_m.group(1).lower()
            # 归一化：gb2312/gbk/gb18030 统一用 gbk 解码（gbk 是 gb2312 的超集）
            if detected in ("gb2312", "gb18030"):
                detected = "gbk"
            if detected not in _MANDARIN_ENCODINGS:
                detected = "utf-8"  # 不认识的编码 → 回退 UTF-8
            try:
                return raw_bytes.decode(detected)
            except (UnicodeDecodeError, LookupError):
                pass  # meta 声明的编码无效 → 继续 heuristic
    except Exception:
        pass

    # ── 步骤②：中文字符密度评分（heuristic fallback）──
    # 遍历候选编码，用 CJK Unified Ideographs (U+4E00–U+9FFF) 密度打分
    best_text = ""
    best_score = -1
    for enc in _MANDARIN_ENCODINGS:
        try:
            text = raw_bytes.decode(enc)
            # 统计中文字符数量（基本汉字区 + 扩展A区）
            cjk_count = sum(
                1 for ch in text
                if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf'
            )
            # 中文字符密度 = CJK字符数 / 总字符数
            score = cjk_count / max(len(text), 1)
            if score > best_score:
                best_score = score
                best_text = text
        except (UnicodeDecodeError, LookupError):
            continue

    # 如果所有编码都失败（极端情况），用 UTF-8 + errors=replace 兜底
    if not best_text:
        return raw_bytes.decode("utf-8", errors="replace")
    return best_text


def _parse_sogou(html: str, max_results: int) -> list[dict]:
    """从 search.sogou.com 搜索结果页提取搜索结果。

    Sogou 搜索结果结构（2025年7月）：
    每条结果包裹在 class="vrwrap" 的 div 中，
    内嵌 <a id="sogou_vr_xxx" href="URL">标题</a> + <p class="star-wiki">摘要</p>。

    ⚠️ Sogou 改版时此解析可能失效——届时需更新正则。
    """
    results: list[dict] = []
    # Sogou 结果块识别：vrwrap 容器 + vrTitle 标题区
    blocks = re.split(r'class="vrwrap"', html)[1:]
    for block in blocks[:max_results]:
        # 提取标题链接
        title_m = re.search(
            r'<a[^>]*id="sogou_vr[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            block, re.S,
        )
        # 提取摘要（star-wiki 类或普通 p 标签）
        snippet_m = re.search(
            r'class="(?:star-wiki|str-text)[^"]*"[^>]*>(.*?)</(?:p|div)>',
            block, re.S,
        )
        url = title_m.group(1) if title_m else ""
        title = _clean_html(title_m.group(2)) if title_m else ""
        snippet = _clean_html(snippet_m.group(1)) if snippet_m else ""
        if title or url:
            results.append({
                "title": title.strip(),
                "url": url.strip(),
                "snippet": snippet.strip(),
            })
    return results


def _synthesize_urls(query: str, max_results: int) -> list[dict]:
    """终极兜底——直接构造内容页 URL，而非搜索引擎结果页。

    【2026-07-29 修复 v2】上一版生成的是 sogou.com/web?query=site:zhihu.com+关键词，
    这是搜索引擎结果页 → web_fetch + 内容门禁（Fix 1）会直接丢弃（CJK<200）。

    新策略：跳过搜索引擎，直接构造已知内容平台的内容页 URL。
    这些 URL 指向的页面是服务端渲染的、CJK 密集的、有实际分析价值的。

    构造规则（按优先级）：
      1. 知乎搜索页（server-rendered，有摘要，CJK 密集）
      2. 提取 query 中第一个词作为站内搜索关键词
      3. 如果 query 含知名产品名，构造特定的内容来源 URL
    绝不构造搜索引擎结果页 URL。
    """
    from urllib.parse import quote

    results: list[dict] = []
    encoded_q = quote(query)

    # ── 策略1：知乎搜索（服务端渲染，中文内容密度高） ──
    # zhihu.com/search?type=content → 不依赖 JS，服务器直接返回 HTML
    # 每个搜索结果包含标题 + 摘要（150~300字），CJK 密度 > 80%
    results.append({
        "title": f"[知乎搜索] {query}",
        "url": f"https://www.zhihu.com/search?type=content&q={encoded_q}",
        "snippet": f"知乎上关于 '{query}' 的讨论",
    })

    # ── 策略2：提取核心关键词搜索更多平台 ──
    if len(results) < max_results:
        # 取 query 第一个词作为主关键词（如 "王者荣耀 美术" → "王者荣耀"）
        main_term = query.split()[0] if query.strip() else query
        main_encoded = quote(main_term)

        # 2a. 知乎话题页
        if len(results) < max_results:
            results.append({
                "title": f"[知乎话题] {main_term}",
                "url": f"https://www.zhihu.com/search?type=topic&q={main_encoded}",
                "snippet": f"知乎上关于 {main_term} 的话题讨论",
            })

        # 2b. 少数派（服务端渲染的应用评测站）
        if len(results) < max_results:
            results.append({
                "title": f"[少数派] {main_term}",
                "url": f"https://sspai.com/search/post/{main_encoded}",
                "snippet": f"少数派上关于 {main_term} 的评测文章",
            })

    return results


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
