# -*- coding: utf-8 -*-
"""MCP 工具统一管控守卫测试。

守的是「统一管控真的统一」这件事：

  1. 白名单、参数校验、审计只有一份实现（invoke），call_tool 只是协议包装；
  2. 无论走 invoke 还是 call_tool，拒绝行为与审计口径一致；
  3. 参数摘要必须截断 —— 检索参数里带的是整批 chunk 正文，原样落库会把
     agent_logs 撑爆；
  4. 审计失败不能影响工具执行结果（审计是辅助链路）。

不连库：审计用假对象收集调用。
"""

from __future__ import annotations

import asyncio

import pytest

from src.mcp.server import (
    MCPServer,
    ToolArgumentError,
    ToolNotFound,
    _digest_arguments,
    _summarize_result,
)


class _FakeAudit:
    """鸭子类型的审计出口：只收集调用，不写库。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def log(self, **kwargs) -> None:
        self.calls.append(kwargs)


async def _echo_handler(args: dict) -> dict:
    return {"echo": args["text"]}


async def _boom_handler(args: dict) -> dict:
    raise RuntimeError("工具内部炸了")


async def _list_handler(args: dict) -> list:
    return [{"index": 0, "score": 0.9}, {"index": 1, "score": 0.5}, {"index": 2, "score": 0.5}]


def _build(audit=None, handler=_echo_handler, name="echo") -> MCPServer:
    server = MCPServer(settings=None, audit=audit)
    server.register(
        name=name,
        description="测试用",
        parameters={
            "required": ["text"],
            "properties": {
                "text": {"type": "string"},
                "n": {"type": "integer"},
            },
        },
        handler=handler,
    )
    return server


# ── 统一执行层 ──────────────────────────────────────────────────────────


def test_invoke返回原生对象而不是MCP包装():
    server = _build()
    result = asyncio.run(server.invoke("echo", {"text": "hi"}, task_id="t1", agent_name="collector"))
    assert result == {"echo": "hi"}


def test_call_tool返回MCP协议格式():
    server = _build()
    result = asyncio.run(server.call_tool("echo", {"text": "hi"}))
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    assert '"hi"' in result["content"][0]["text"]


def test_白名单之外的工具被拒绝并留痕():
    audit = _FakeAudit()
    server = _build(audit=audit)

    with pytest.raises(ToolNotFound):
        asyncio.run(server.invoke("nope", {}, task_id="t1", agent_name="collector"))

    assert len(audit.calls) == 1
    assert "ToolNotFound" in audit.calls[0]["error"]
    assert audit.calls[0]["action"] == "tool_call"


def test_缺必填参数被拒绝():
    server = _build()
    with pytest.raises(ToolArgumentError):
        asyncio.run(server.invoke("echo", {"n": 3}))


def test_参数类型不符被拒绝():
    server = _build()
    with pytest.raises(ToolArgumentError):
        asyncio.run(server.invoke("echo", {"text": "hi", "n": "3"}))


def test_bool不能冒充integer():
    server = _build()
    with pytest.raises(ToolArgumentError):
        asyncio.run(server.invoke("echo", {"text": "hi", "n": True}))


def test_未声明的额外参数放行():
    server = _build()
    result = asyncio.run(server.invoke("echo", {"text": "hi", "unknown": 1}))
    assert result == {"echo": "hi"}


def test_工具异常透传但审计记错误():
    audit = _FakeAudit()
    server = _build(audit=audit, handler=_boom_handler)

    with pytest.raises(RuntimeError):
        asyncio.run(server.invoke("echo", {"text": "hi"}, task_id="t1", agent_name="collector"))

    assert audit.calls[0]["error"].startswith("RuntimeError")
    assert audit.calls[0]["response"] == {"ok": False}


def test_call_tool把异常转成isError():
    server = _build(handler=_boom_handler)
    result = asyncio.run(server.call_tool("echo", {"text": "hi"}))
    assert result["isError"] is True
    assert "工具内部炸了" in result["content"][0]["text"]


def test_没有审计出口时正常执行():
    server = _build(audit=None)
    assert asyncio.run(server.invoke("echo", {"text": "hi"}, task_id="t1")) == {"echo": "hi"}


def test_没有task_id时不写审计():
    audit = _FakeAudit()
    server = _build(audit=audit)
    asyncio.run(server.invoke("echo", {"text": "hi"}))  # 不传 task_id
    assert audit.calls == []


def test_成功调用也写审计并带结果摘要():
    audit = _FakeAudit()
    server = _build(audit=audit, handler=_list_handler)
    asyncio.run(server.invoke("echo", {"text": "hi"}, task_id="t1", agent_name="analyzer"))

    call = audit.calls[0]
    assert call["agent_name"] == "analyzer"
    assert call["response"]["ok"] is True
    assert call["response"]["n"] == 3
    assert call["response"]["score_top1"] == 0.9


# ── 摘要与结果压缩 ──────────────────────────────────────────────────────


def test_参数摘要截断长字符串():
    digest = _digest_arguments({"query": "x" * 500})
    assert digest["query"].endswith("(len=500)")
    assert len(digest["query"]) < 200


def test_参数摘要只记列表长度不记正文():
    digest = _digest_arguments({"documents": ["长文本" * 100] * 50})
    assert digest["documents"]["list_len"] == 50
    assert "长文本" not in str(digest["documents"])


def test_结果摘要提取分数分布():
    summary = _summarize_result([{"score": 0.9}, {"score": 0.5}, {"score": 0.5}])
    assert summary["n"] == 3
    assert summary["score_top1"] == 0.9
    assert summary["score_median"] == 0.5


def test_结果摘要记正文长度():
    assert _summarize_result({"text_content": "abc"})["chars"] == 3


def test_结果摘要在无分数时只给条数():
    summary = _summarize_result([{"title": "a"}, {"title": "b"}])
    assert summary == {"kind": "list", "n": 2}
