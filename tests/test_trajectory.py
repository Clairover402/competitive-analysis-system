# -*- coding: utf-8 -*-
"""轨迹评估守卫测试。

守的是三条「轨迹一旦读错就全盘错」的性质：

  1. 分类只认 action：think / observe / tool_call / 其余，不能靠猜；
  2. 缺数据必须回 n/a 而不是 0 —— 老任务没有 tool_call 日志是常态，
     把「没记上」当「没发生」会把健康任务判成有病；
  3. 根因能落到具体那一步（第几轮重复、哪类抓取失败），只给分数等于没诊断。

全部构造 dict 喂给纯函数，不连库、不调 LLM。
"""

from __future__ import annotations

from src.evaluation import trajectory as tj

# ── 构造样本 ────────────────────────────────────────────────────────────


def _row(action: str, *, agent: str = "supervisor", request=None, response=None,
         error=None, duration_ms=None, seq: int = 0) -> dict:
    return {
        "task_id": "t-1",
        "created_at": f"2026-10-19T10:00:{seq:02d}",
        "agent_name": agent,
        "action": action,
        "request": request or {},
        "response": response or {},
        "error": error,
        "duration_ms": duration_ms,
    }


def _think(round_: int, agent: str, args: dict | None = None, seq: int | None = None) -> dict:
    """一条 think 日志：日志 action='think'，决策结果在 response 里。"""
    return _row(
        "think",
        request={"round": round_},
        response={
            "round": round_,
            "action": agent,
            "agent": agent,
            "thought": f"round {round_}",
            "reason": "test",
            "arguments": args or {},
        },
        seq=seq if seq is not None else round_ * 3,
    )


def _observe(round_: int, agent: str, status: str = "completed", obs: str = "ok",
             seq: int | None = None) -> dict:
    return _row(
        "observe",
        request={"round": round_, "agent": agent, "status": status},
        response={"observation": obs},
        seq=seq if seq is not None else round_ * 3 + 1,
    )


def _tool(name: str, args: dict | None = None, *, ok: bool = True, n=None,
          chars=None, error=None, scores=None, seq: int = 90) -> dict:
    response: dict = {"ok": ok}
    if n is not None:
        response["n"] = n
    if chars is not None:
        response["chars"] = chars
    if scores:
        ordered = sorted(scores, reverse=True)
        response["score_top1"] = ordered[0]
        response["score_median"] = ordered[len(ordered) // 2]
        response["score_min"] = ordered[-1]
    return _row(
        "tool_call",
        agent="collector",
        request={"tool": name, "args": args or {}},
        response=response,
        error=error,
        duration_ms=12.0,
        seq=seq,
    )


# ── 分类与重建 ──────────────────────────────────────────────────────────


def test_按action分类_四类事件各归各位():
    rows = [
        _think(1, "collector", {"competitors": "list(len=2)"}),
        _observe(1, "collector", obs="collected 2 competitors"),
        _tool("web_search", {"query": "飞书 定价"}, n=8),
        _row("dim_analysis", agent="analyzer", request={"dimension": "定价"}),
    ]
    trace = tj.build_trace("t-1", rows)

    assert len(trace.thinks) == 1
    assert len(trace.observes) == 1
    assert len(trace.tools) == 1
    assert len(trace.business) == 1
    assert trace.is_supervisor is True
    assert trace.raw_count == 4


def test_没有think日志时判为非supervisor():
    trace = tj.build_trace("t-2", [_row("collect_pages", agent="collector")])
    assert trace.is_supervisor is False


def test_jsonb被回成字符串也能解析():
    row = _row("think", request={"round": 1})
    row["response"] = '{"round": 1, "action": "finish", "agent": "finish", "arguments": {}}'
    trace = tj.build_trace("t-3", [row])
    assert trace.thinks[0]["agent"] == "finish"


# ── L1 指标 ─────────────────────────────────────────────────────────────


def test_正常完成_终止原因是finish且轮次效率满():
    rows = [
        _think(1, "collector"), _observe(1, "collector"),
        _think(2, "analyzer"), _observe(2, "analyzer"),
        _think(3, "finish"), _observe(3, "finish", obs=""),
    ]
    l1 = tj.l1_metrics(tj.build_trace("t", rows))

    assert l1["终止原因"]["value"] == "finish"
    # finish 轮不产生新业务状态 → 有效轮次 = 2（collector/analyzer）/ 总轮次 3
    assert l1["轮次效率"]["value"] == round(2 / 3, 3)
    assert l1["非法转移"]["value"] == 0


def test_轮次耗尽识别为max_rounds():
    rows = []
    for i in range(1, tj.DEFAULT_MAX_ROUNDS + 1):
        rows += [_think(i, "collector"), _observe(i, "collector")]
    l1 = tj.l1_metrics(tj.build_trace("t", rows))
    assert l1["终止原因"]["value"] == "max_rounds"


def test_analyzer在collector之前记为非法转移():
    rows = [_think(1, "analyzer"), _observe(1, "analyzer"), _think(2, "finish")]
    l1 = tj.l1_metrics(tj.build_trace("t", rows))
    assert l1["非法转移"]["value"] == 1
    assert "analyzer" in l1["非法转移"]["note"]


def test_连续同agent同参数算重复动作():
    same = {"competitors": "list(len=2)"}
    rows = [
        _think(1, "collector", same), _observe(1, "collector"),
        _think(2, "collector", same), _observe(2, "collector"),
        _think(3, "finish"),
    ]
    l1 = tj.l1_metrics(tj.build_trace("t", rows))
    assert l1["重复动作率"]["value"] == 0.5          # 2 次相邻比较里 1 次重复


def test_失败后原样重试计入观察响应性():
    same = {"competitors": "list(len=2)"}
    rows = [
        _think(1, "collector", same), _observe(1, "collector", status="failed", obs="collector failed"),
        _think(2, "collector", same), _observe(2, "collector"),
        _think(3, "finish"),
    ]
    l1 = tj.l1_metrics(tj.build_trace("t", rows))
    assert l1["观察响应性"]["value"] == 1.0


def test_必填参数缺键会计入不完整():
    rows = [
        _think(1, "collector", {}),          # 缺 competitors/dimensions
        _observe(1, "collector"),
        _think(2, "finish"),
    ]
    l1 = tj.l1_metrics(tj.build_trace("t", rows))
    assert l1["参数完整率"]["value"] == 0.0


# ── L2 指标 ─────────────────────────────────────────────────────────────


def test_搜索空结果率():
    rows = [
        _tool("web_search", {"query": "a"}, n=0, seq=1),
        _tool("web_search", {"query": "b"}, n=5, seq=2),
        _tool("web_search", {"query": "c"}, n=3, seq=3),
        _tool("web_search", {"query": "d"}, n=0, seq=4),
    ]
    l2 = tj.l2_metrics(tj.build_trace("t", rows))
    assert l2["搜索空结果率"]["value"] == 0.5


def test_抓取失败按错误类型分桶():
    rows = [
        _tool("web_fetch", {"url": "https://a"}, ok=False, error="TimeoutError: read timeout", seq=1),
        _tool("web_fetch", {"url": "https://b"}, ok=False, error="HTTPStatusError: 403 Forbidden", seq=2),
        _tool("web_fetch", {"url": "https://c"}, ok=True, chars=1200, seq=3),
    ]
    l2 = tj.l2_metrics(tj.build_trace("t", rows))
    note = l2["抓取失败率"]["note"]
    assert l2["抓取失败率"]["value"] == round(2 / 3, 3)
    assert "timeout" in note and "403" in note


def test_重复抓取率():
    rows = [
        _tool("web_fetch", {"url": "https://a"}, chars=100, seq=1),
        _tool("web_fetch", {"url": "https://a"}, chars=100, seq=2),
    ]
    l2 = tj.l2_metrics(tj.build_trace("t", rows))
    assert l2["重复抓取率"]["value"] == 0.5


def test_embedding调用次数与批大小():
    rows = [
        _tool("embed_query", {"query": "定价"}, n=None, seq=1),
        _tool("embed_texts", {"texts": "list(len=12)"}, n=12, seq=2),
        _tool("embed_texts", {"texts": "list(len=8)"}, n=8, seq=3),
    ]
    l2 = tj.l2_metrics(tj.build_trace("t", rows))
    assert l2["embedding 调用"]["value"] == 3
    assert "20 条" in l2["embedding 调用"]["note"]


def test_rerank区分度取top1减中位():
    rows = [
        _tool("rerank", {"query": "飞书 定价", "top_k": 15}, n=15,
              scores=[0.91, 0.90, 0.90, 0.89, 0.89], seq=1),
    ]
    l2 = tj.l2_metrics(tj.build_trace("t", rows))
    # 排序后 [0.91, 0.90, 0.90, 0.89, 0.89]，median 取 index 2 = 0.90
    assert l2["rerank 区分度"]["value"] == 0.01


def test_采集漏斗串起搜索抓取入库():
    rows = [
        _tool("web_search", {"query": "a"}, n=10, seq=1),
        _tool("web_fetch", {"url": "https://a"}, chars=3000, seq=2),
        _tool("web_fetch", {"url": "https://b"}, chars=0, seq=3),
        _tool("embed_texts", {"texts": "list(len=4)"}, n=4, seq=4),
    ]
    l2 = tj.l2_metrics(tj.build_trace("t", rows))
    assert l2["采集漏斗"]["value"] == "10 → 2 → 1 → 4"


def test_没有工具日志时全部n_a而不是0():
    trace = tj.build_trace("t", [_think(1, "finish")])
    l2 = tj.l2_metrics(trace)
    assert all(item["value"] == "n/a" for item in l2.values())


# ── 渲染 ────────────────────────────────────────────────────────────────


def test_单任务报告包含三层结构与结论():
    rows = [
        _think(1, "collector", {"competitors": "list(len=2)", "dimensions": "list(len=2)"}),
        _observe(1, "collector", obs="collected 2 competitors"),
        _think(2, "finish"),
    ]
    text = tj.render_case(tj.build_trace("t-9", rows))
    assert "L1 编排级" in text and "L2 工具级" in text
    assert "结论" in text
    assert "t-9" in text


def test_pipeline任务结论提示去看采集侧():
    trace = tj.build_trace("t", [_row("collect_pages", agent="collector")])
    text = tj.render_case(trace)
    assert "Pipeline" in text


def test_批量报告不崩且给出分布():
    traces = [
        tj.build_trace("a", [_think(1, "finish")]),
        tj.build_trace("b", [_think(1, "collector"), _observe(1, "collector")]),
    ]
    text = tj.render_batch(traces)
    assert "终止原因分布" in text
    assert "阈值未定" in text


def test_parse_since支持d_h_和ISO():
    assert tj._parse_since("7d").year >= 2020
    assert tj._parse_since("24h").year >= 2020
    assert tj._parse_since("2026-10-01").month == 10
