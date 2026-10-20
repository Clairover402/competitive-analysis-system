# -*- coding: utf-8 -*-
"""Supervisor 轨迹埋点测试 —— 强制结束 / JSON 降级必须各留一条日志。

守的性质：轨迹评估判 termination 只认 agent_logs。如果这两条路径只在返回值里
体现、不落日志，评估侧看到的事实就变了味：

  · 重写超限 → 最后一条 think 仍是 writer → 被判成 incomplete（症状对、原因错）
  · JSON 降级 → think 写着 action=finish → 被混进「LLM 主动完成」，健康度虚高

只 import 两个节点工厂，不建图、不调真 LLM、不连库。
"""

from __future__ import annotations

import asyncio

from src.supervisor.supervisor import (
    _MAX_REWRITES,
    _make_node_act,
    _make_node_think,
)


class _StubLogDAO:
    """记录写入的日志，不落库。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def log(self, **kwargs) -> None:
        self.calls.append(kwargs)


class _StubRouter:
    def list_capabilities(self) -> str:
        return "- collector: 采集\n- analyzer: 分析"


class _BadJSONResponse:
    content = "抱歉，我无法提供 JSON 格式的回复。"


class _StubLLM:
    """永远返回非 JSON —— 触发 think 的「两次解析失败」降级路径。"""

    async def ainvoke(self, prompt):
        return _BadJSONResponse()


def test_重写超限时写一条forced_finish日志():
    dao = _StubLogDAO()
    node = _make_node_act(_StubRouter(), log_dao=dao)

    out = asyncio.run(node({
        "task_id": "t-1",
        "current_round": 6,
        "rewrite_count": _MAX_REWRITES,
        "pending_decision": {
            "action": "writer",
            "agent": "writer",
            "arguments": {"title": "x"},
        },
    }))

    # 行为不变：仍然强制结束
    assert out["pending_task_agent"] == "finish"
    assert out["is_complete"] is True
    # 新增：留痕，且带得上判断所需的字段
    assert len(dao.calls) == 1
    log = dao.calls[0]
    assert log["action"] == "forced_finish"
    assert log["request"]["rewrite_count"] == _MAX_REWRITES
    assert log["request"]["round"] == 6
    assert log["task_id"] == "t-1"


def test_未超限时不写forced_finish日志():
    # 写日志只在硬拦截分支——正常调度不能凭空多一条 forced_finish
    dao = _StubLogDAO()
    node = _make_node_act(_StubRouter(), log_dao=dao)

    asyncio.run(node({
        "task_id": "t-2",
        "current_round": 2,
        "rewrite_count": 0,
        "pending_decision": {"action": "finish", "agent": "finish", "arguments": {}},
    }))

    assert dao.calls == []


def test_json两次解析失败时think写degraded日志():
    dao = _StubLogDAO()
    node = _make_node_think(_StubLLM(), _StubRouter(), None, None, dao)

    out = asyncio.run(node({"task_id": "t-3", "current_round": 4}))

    # 行为不变：仍然安全退出
    assert out["is_complete"] is True
    assert out["pending_decision"]["action"] == "finish"
    # 新增：降级必须可在日志里识别，否则会被当成正常完成
    assert len(dao.calls) == 1
    log = dao.calls[0]
    assert log["action"] == "think"
    assert log["response"]["degraded"] == "json_error"
    assert log["request"]["round"] == 4


def test_没有log_dao时两条路径都不报错():
    # log_dao 是可选依赖：不传也得能跑（单测 / 无审计场景）
    act = _make_node_act(_StubRouter())
    think = _make_node_think(_StubLLM(), _StubRouter(), None, None, None)

    out_act = asyncio.run(act({
        "task_id": "t-4",
        "current_round": 1,
        "rewrite_count": _MAX_REWRITES,
        "pending_decision": {"action": "writer", "arguments": {}},
    }))
    out_think = asyncio.run(think({"task_id": "t-5", "current_round": 1}))

    assert out_act["is_complete"] is True
    assert out_think["is_complete"] is True
