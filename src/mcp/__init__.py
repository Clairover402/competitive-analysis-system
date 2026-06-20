"""MCP 工具层 — Agent 能力工具箱。
============================================================

【L3 架构】预注册 5 个工具，每个工具的定位：
  web_search  — Collector 的眼睛（互联网搜索）
  web_fetch   — Collector 的手（网页内容抓取）
  embed_texts — Analyzer 的思维（语义理解）
  embed_query — Analyzer 的输入（查询向量化）
  rerank      — Analyzer 的判断（精排挑选最相关）

【L5 面试答案模板】"MCP 在你们系统里怎么落地的？"
→ "我们用 create_mcp_server() 工厂函数预注册 5 个工具，
   Supervisor 架构中的所有 Agent 共享同一个 MCPServer 实例。
   每个 Agent 的 System Prompt 里注入 tools/list 结果，
   LLM 根据任务自动选择工具——这是 MCP N×M→N+M 的工程落地。"

用法:
    from src.mcp import create_mcp_server
    server = create_mcp_server(settings)
    result = await server.call_tool("web_search", {"query": "飞书"})
"""

from __future__ import annotations

from src.config import Settings
from src.mcp.server import MCPServer
from src.mcp.tools_web import web_search, web_fetch
from src.mcp.tools_rag import embed_texts, embed_query, rerank


def create_mcp_server(settings: Settings | None = None) -> MCPServer:
    """创建并注册全部 5 个工具的 MCP Server。

    【L4 工程】为什么用工厂函数不用类？
    ------------------------------------------------------------
    工厂函数 = 一个调用完成"创建 + 注册"，不需要两步操作。
    避免外部忘记 register，导致裸的 MCPServer 流入 Agent。

    面试官："为什么不把 register 写在 MCPServer.__init__ 里？"
    → "那样会把工具注册硬编码到 Server 类里——
      测试时想换工具就改不了。工厂函数让注册逻辑和 Server 解耦，
      测试时可以 create_mcp_server_with_mocks() 注入假工具。"

    Args:
        settings: 配置实例，为 None 时使用默认值。

    Returns:
        已注册 5 个工具的 MCPServer 实例。
    """
    server = MCPServer(settings=settings)

    # 1. web_search —— 联网搜索
    server.register(
        name="web_search",
        description="搜索互联网获取信息，返回标题、URL 与摘要列表",
        parameters={
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词",
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回数量",
                    "default": 10,
                },
            },
        },
        # lambda 适配：MCP handler(dict) → 工具函数(query, max_results, ...)
        handler=lambda args: web_search(
            query=args["query"],
            max_results=args.get("max_results", 10),
            settings=settings,
        ),
    )

    # 2. web_fetch —— 网页抓取
    server.register(
        name="web_fetch",
        description="抓取网页内容并提取正文文本",
        parameters={
            "required": ["url"],
            "properties": {
                "url": {
                    "type": "string",
                    "description": "目标网页 URL",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "最大提取字符数",
                    "default": 10000,
                },
            },
        },
        handler=lambda args: web_fetch(
            url=args["url"],
            max_chars=args.get("max_chars", 10000),
            settings=settings,
        ),
    )

    # 3. embed_texts —— 批量文本嵌入
    # 注意：model 约 2GB，首次调用 5-10 秒加载，后续 < 100ms
    server.register(
        name="embed_texts",
        description="将文本批量转换为 BGE-M3 1024 维向量（懒加载 ~2GB 模型）",
        parameters={
            "required": ["texts"],
            "properties": {
                "texts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "待嵌入的文本列表",
                },
            },
        },
        handler=lambda args: embed_texts(
            texts=args["texts"],
            settings=settings,
        ),
    )

    # 4. embed_query —— 单条查询嵌入
    # 实际委托 embed_texts([query])[0]，共享同一个懒加载模型
    server.register(
        name="embed_query",
        description="将单条查询文本转换为 BGE-M3 1024 维向量",
        parameters={
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "查询文本",
                },
            },
        },
        handler=lambda args: embed_query(
            query=args["query"],
            settings=settings,
        ),
    )

    # 5. rerank —— 精排（bi-encoder 粗排后的 cross-encoder 精排）
    # 输入：query + documents 列表
    # 输出：[{index, text, score}, ...] 按分数降序
    server.register(
        name="rerank",
        description="使用 BGE-reranker-v2-m3 对检索结果精排",
        parameters={
            "required": ["query", "documents"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "查询文本",
                },
                "documents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "待排序文档列表",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回数量",
                    "default": 10,
                },
            },
        },
        handler=lambda args: rerank(
            query=args["query"],
            documents=args["documents"],
            top_k=args.get("top_k", 10),
            settings=settings,
        ),
    )

    return server


# 【L4 工程】__all__ 控制 from src.mcp import * 的导出范围
__all__ = [
    "MCPServer",
    "create_mcp_server",
    "web_search",
    "web_fetch",
    "embed_texts",
    "embed_query",
    "rerank",
]
